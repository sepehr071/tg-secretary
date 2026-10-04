"""Dashboard login: one-time links, hashed sessions, request guards.

Login tokens and sessions live in bot_state as sha256 hashes with an expiry
epoch as the value, so a copied DB holds no usable session and sessions
survive pm2 restarts.
"""
from __future__ import annotations

import hashlib
import secrets
import time

from .. import db

LOGIN_TTL = 3600
SESSION_TTL = 7 * 86400
COOKIE = "tgs_session"
LOGIN_PREFIX = "dash_login:"
SESSION_PREFIX = "dash_session:"
_LOCAL_NAMES = {"127.0.0.1", "localhost", "[::1]"}


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def host_ok(host: str) -> bool:
    """Loopback hostnames only, any port: blocks DNS rebinding, allows ssh -L 9000:..."""
    name = host if host.endswith("]") else host.rsplit(":", 1)[0]
    return name.lower() in _LOCAL_NAMES


def origin_ok(origin: str | None, host: str) -> bool:
    """POSTs must come from a page this dashboard served (CSRF guard)."""
    return origin == f"http://{host}"


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
