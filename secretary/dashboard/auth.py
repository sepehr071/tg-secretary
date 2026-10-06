"""Dashboard login: one-time links, hashed sessions, request guards.

Login tokens and sessions live in bot_state as sha256 hashes with an expiry
epoch as the value, so a copied DB holds no usable session and sessions
survive pm2 restarts.
"""
from __future__ import annotations

import hashlib
import secrets
import time
from urllib.parse import urlsplit

from .. import db
from ..config import settings

LOGIN_TTL = 3600
SESSION_TTL = 7 * 86400
COOKIE = "tgs_session"
LOGIN_PREFIX = "dash_login:"
SESSION_PREFIX = "dash_session:"
_LOCAL_NAMES = {"127.0.0.1", "localhost", "[::1]"}


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def host_ok(host: str) -> bool:
    """Loopback names (any port, so ssh -L 9000:... works) or the configured public
    host; anything else is refused, which blocks DNS rebinding."""
    name = (host if host.endswith("]") else host.rsplit(":", 1)[0]).lower()
    public = (urlsplit(settings.dashboard_public_url).hostname or "").lower()
    return name in _LOCAL_NAMES or (bool(public) and name == public)


def origin_ok(origin: str | None, host: str) -> bool:
    """POSTs must come from a page this dashboard served (CSRF guard); https covers a
    TLS reverse proxy in front."""
    return origin in (f"http://{host}", f"https://{host}")


def proxy_ok(header: str | None) -> bool:
    """Hosted mode: the hosting proxy proves itself with the per-tenant shared secret."""
    secret = settings.dashboard_proxy_secret
    return bool(secret) and secrets.compare_digest((header or "").encode(), secret.encode())


async def create_login_token(ttl: int = LOGIN_TTL) -> str:
    token = secrets.token_urlsafe(32)
    await db.set_state(LOGIN_PREFIX + _hash(token), str(int(time.time()) + ttl))
    return token


async def redeem_login_token(token: str) -> str | None:
    """Spend a login token (works once). Returns a new session token, or None."""
    now = int(time.time())
    await db.purge_expired_state(LOGIN_PREFIX, now)
    await db.purge_expired_state(SESSION_PREFIX, now)
    expires = await db.pop_state(LOGIN_PREFIX + _hash(token))
    if expires is None or int(expires) < now:
        return None
    session = secrets.token_urlsafe(32)
    await db.set_state(SESSION_PREFIX + _hash(session), str(now + SESSION_TTL))
    return session


async def session_valid(session: str | None) -> bool:
    if not session:
        return False
    expires = await db.get_state(SESSION_PREFIX + _hash(session))
    return expires is not None and int(expires) > time.time()


async def end_session(session: str | None) -> None:
    if session:
        await db.pop_state(SESSION_PREFIX + _hash(session))
