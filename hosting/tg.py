"""Minimal Bot API caller for the platform bot and for checking tenant tokens."""
from __future__ import annotations

from typing import Any

import httpx

TRANSPORT: httpx.AsyncBaseTransport | None = None


class TelegramError(Exception):
    pass


async def call(token: str, method: str, **params: Any) -> Any:
    timeout = params.get("timeout", 0) + 15
    async with httpx.AsyncClient(timeout=timeout, transport=TRANSPORT) as c:
        r = await c.post(f"https://api.telegram.org/bot{token}/{method}", json=params)
    try:
        data = r.json()
    except ValueError:
        raise TelegramError(f"{method}: HTTP {r.status_code}") from None
    if not data.get("ok"):
        raise TelegramError(f"{method}: {data.get('description', r.status_code)}")
    return data["result"]
