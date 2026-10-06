"""Shared platform bot: routes every update to the owning tenant, answers strangers' /start, warns on low credit."""
from __future__ import annotations

import asyncio
import logging

import httpx

from . import db, openrouter, router, tg
from .config import settings

log = logging.getLogger(__name__)
CREDIT_CHECK_SECONDS = 1800


async def _say(chat_id: int, text: str) -> None:
    try:
        await tg.call(settings.platform_bot_token, "sendMessage", chat_id=chat_id, text=text)
    except (tg.TelegramError, httpx.HTTPError) as e:  # httpx errors may carry the URL/token
        log.warning("platform bot DM failed: %s", e if isinstance(e, tg.TelegramError) else type(e).__name__)


ALLOWED_UPDATES = ["message", "callback_query", "business_connection", "business_message",
                   "edited_business_message", "deleted_business_messages"]


async def handle_update(u: dict) -> None:
    if await router.dispatch(u) != "platform":
        return
    msg = u.get("message") or {}
    if (msg.get("text") or "").startswith("/start"):
        await _say(msg["chat"]["id"], f"برای ساخت منشی خودتان وارد سایت شوید:\n{settings.public_url}")


async def check_credit_once() -> None:
    for t in await db.list_tenants():
        if t["status"] != "running" or not t["or_key_hash"]:
            continue
        try:
            info = await openrouter.get_key(t["or_key_hash"])
        except openrouter.OpenRouterError as e:
            log.warning("credit check failed for tenant %s: %s", t["id"], e)
            continue
        if info.get("limit_remaining") is None:
            continue
        limit = float(info.get("limit") or 0)
        remaining = float(info["limit_remaining"])
        if limit and remaining < 0.2 * limit and t["warned_at_limit"] != limit:
            await _say(t["owner_tg_id"], f"اعتبار منشی شما رو به اتمام است (باقی\u200cمانده: ${remaining:.2f}). "
                                         f"برای شارژ: {settings.public_url}/account")
            await db.update_tenant(t["id"], warned_at_limit=limit)


async def _credit_loop() -> None:
    while True:
        try:
            await check_credit_once()
        except Exception as e:  # noqa: BLE001 — keep the loop alive
            log.warning("credit loop: %s", type(e).__name__)
        await asyncio.sleep(CREDIT_CHECK_SECONDS)


async def run_platform_bot() -> None:
    credit = asyncio.create_task(_credit_loop())
    offset = 0
    try:
        while True:
            try:
                updates = await tg.call(settings.platform_bot_token, "getUpdates", offset=offset,
                                        timeout=30, allowed_updates=ALLOWED_UPDATES)
            except tg.TelegramError as e:  # "method: description", never the URL
                log.warning("getUpdates failed: %s", e)
                await asyncio.sleep(5)
                continue
            except Exception as e:  # noqa: BLE001
                log.warning("getUpdates failed: %s", type(e).__name__)  # httpx errors may carry the URL/token
                await asyncio.sleep(5)
                continue
            for u in updates:
                offset = u["update_id"] + 1
                try:
                    await handle_update(u)
                except Exception as e:  # noqa: BLE001 — one bad update must not stop the loop
                    log.warning("update %s failed: %s", u.get("update_id"), type(e).__name__)  # no traceback: may carry the token URL
    finally:
        credit.cancel()
