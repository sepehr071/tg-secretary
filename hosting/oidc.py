"""Log in with Telegram (OIDC, Authorization Code + PKCE)."""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from typing import Any
from urllib.parse import urlencode

import httpx

from .config import settings

AUTH_URL = "https://oauth.telegram.org/auth"
TOKEN_URL = "https://oauth.telegram.org/token"
ISSUER = "https://oauth.telegram.org"
REDIRECT_PATH = "/auth/callback"
TRANSPORT: httpx.AsyncBaseTransport | None = None


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def new_pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    return verifier, _b64url(hashlib.sha256(verifier.encode()).digest())


def redirect_uri() -> str:
    return settings.public_url + REDIRECT_PATH


def auth_url(state: str, challenge: str) -> str:
    return AUTH_URL + "?" + urlencode({
        "client_id": settings.oidc_client_id, "redirect_uri": redirect_uri(),
        "response_type": "code", "scope": "openid profile telegram:bot_access",
        "state": state, "code_challenge": challenge, "code_challenge_method": "S256",
    })


def claims_from_id_token(id_token: str, now: float) -> dict[str, Any]:
    # ponytail: no signature check — the token comes straight from Telegram's token
    # endpoint over TLS with our client secret (OIDC Core 3.1.3.7). Verify via JWKS if
    # id_tokens ever arrive through the browser.
    try:
        payload = id_token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        if not isinstance(claims, dict):
            raise ValueError("payload is not an object")
    except (IndexError, ValueError, AttributeError, TypeError) as e:
        raise ValueError("malformed id_token") from e
    aud = claims.get("aud")
    if claims.get("iss") != ISSUER:
        raise ValueError("wrong issuer")
    if settings.oidc_client_id not in (aud if isinstance(aud, list) else [aud]):
        raise ValueError("wrong audience")
    try:
        expired = float(claims.get("exp")) <= now
    except (TypeError, ValueError) as e:
        raise ValueError("bad exp") from e
    if expired:
        raise ValueError("expired")
    if "id" not in claims:
        raise ValueError("no telegram id")
    return claims


async def exchange(code: str, verifier: str) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=20, transport=TRANSPORT) as c:
        r = await c.post(TOKEN_URL, auth=(settings.oidc_client_id, settings.oidc_client_secret), data={
            "grant_type": "authorization_code", "code": code,
            "redirect_uri": redirect_uri(), "code_verifier": verifier,
        })
    if r.status_code != 200:
        raise ValueError(f"token endpoint HTTP {r.status_code}")
    try:
        id_token = r.json()["id_token"]
    except (ValueError, KeyError, TypeError) as e:
        raise ValueError("token response has no id_token") from e
    if not isinstance(id_token, str):
        raise ValueError("token response has no id_token")
    return claims_from_id_token(id_token, time.time())
