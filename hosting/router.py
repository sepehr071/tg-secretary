"""Route shared-bot updates to the owning tenant's process (127.0.0.1:<port>/_tg/update)."""
from __future__ import annotations

import logging

import httpx

from . import db, tg
from .config import settings

log = logging.getLogger(__name__)
TRANSPORT: httpx.AsyncBaseTransport | None = None
_BIZ_KEYS = ("business_message", "edited_business_message", "deleted_business_messages")


def _why(e: Exception) -> str:
    return str(e) if isinstance(e, tg.TelegramError) else type(e).__name__  # httpx errors may carry the token URL


async def owner_of(u: dict) -> int | None:
    """Telegram user id whose tenant should get this update, or None."""
    if bc := u.get("business_connection"):
        await db.put_biz_conn(bc["id"], bc["user"]["id"])
        return bc["user"]["id"]
    for key in _BIZ_KEYS:
        if body := u.get(key):
            conn_id = body.get("business_connection_id")
            if not conn_id:
                return None
            owner = await db.get_biz_conn_owner(conn_id)
            if owner is None:
                try:
                    bc = await tg.call(settings.platform_bot_token, "getBusinessConnection",
                                       business_connection_id=conn_id)
                except (tg.TelegramError, httpx.HTTPError) as e:
                    log.warning("getBusinessConnection failed: %s", _why(e))
                    return None
                owner = bc["user"]["id"]
                await db.put_biz_conn(conn_id, owner)
            return owner
    if body := u.get("message"):
        return (body.get("from") or {}).get("id") if (body.get("chat") or {}).get("type") == "private" else None
    if body := u.get("callback_query"):
        return (body.get("from") or {}).get("id")
    return None


async def forward(t: dict, u: dict) -> bool:
    try:
        async with httpx.AsyncClient(transport=TRANSPORT, timeout=10) as c:
            r = await c.post(f"http://127.0.0.1:{t['dashboard_port']}/_tg/update", json=u,
                             headers={"x-platform-auth": t["proxy_secret"],
                                      "x-platform-route": "update"})
    except httpx.HTTPError as e:
        log.warning("forward to tenant %s failed: %s", t["id"], type(e).__name__)
        return False
    if r.status_code >= 300:
        log.warning("tenant %s refused update: HTTP %s", t["id"], r.status_code)
        return False
    return True


async def dispatch(u: dict) -> str:
    owner = await owner_of(u)
    t = await db.get_tenant_by_owner(owner) if owner else None
    if t and t["status"] == "running" and t["dashboard_port"] and t["proxy_secret"]:
        # ponytail: sequential, one slow tenant delays others up to 10 s; create_task + semaphore if it matters
        return "forwarded" if await forward(t, u) else "dropped"
    if "message" in u:
        return "platform" if owner else "dropped"
    bc = u.get("business_connection")
    if bc and t is None and bc.get("is_enabled") and await db.mark_stranger_notified(bc["user"]["id"]):
        try:
            await tg.call(settings.platform_bot_token, "sendMessage", chat_id=bc["user"]["id"],
                          text=f"برای فعال\u200cکردن منشی، اول در سایت ثبت\u200cنام کنید:\n{settings.public_url}")
        except (tg.TelegramError, httpx.HTTPError) as e:
            log.warning("stranger DM failed: %s", _why(e))
    return "dropped"
