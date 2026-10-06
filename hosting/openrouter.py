"""OpenRouter management API: one credit-capped key per tenant."""
from __future__ import annotations

from typing import Any

import httpx

from .config import settings

BASE = "https://openrouter.ai/api/v1"
TRANSPORT: httpx.AsyncBaseTransport | None = None  # tests swap in a MockTransport


class OpenRouterError(Exception):
    pass


async def _req(method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    async with httpx.AsyncClient(base_url=BASE, timeout=20, transport=TRANSPORT,
                                 headers={"Authorization": f"Bearer {settings.openrouter_mgmt_key}"}) as c:
        r = await c.request(method, path, json=body)
    if r.status_code >= 400:
        raise OpenRouterError(f"{method} {path}: HTTP {r.status_code} {r.text[:200]}")
    return r.json()


async def create_key(name: str, limit: float) -> tuple[str, str]:
    """Returns (key, hash). The key is shown only once — write it to the tenant .env at once."""
    d = await _req("POST", "/keys", {"name": name, "limit": limit, "limit_reset": None})
    return d["key"], d["data"]["hash"]


async def get_key(key_hash: str) -> dict[str, Any]:
    return (await _req("GET", f"/keys/{key_hash}"))["data"]


async def set_limit(key_hash: str, limit: float) -> None:
    await _req("PATCH", f"/keys/{key_hash}", {"limit": limit})


async def disable_key(key_hash: str) -> None:
    await _req("PATCH", f"/keys/{key_hash}", {"disabled": True})
