"""HITL drafts: send, edit-and-send, or skip pending replies from the browser."""
from __future__ import annotations

import time

from fastapi import APIRouter, Form, Request

from .. import commands, db
from .contacts import RELATIONSHIPS, display_name
from .web import back, fa_digits, render, t

router = APIRouter()


def _left(seconds: float) -> str:
    m = max(1, int(seconds // 60))
    n, unit = (m // 60, "h") if m >= 60 else (m, "m")
    return fa_digits(f"{n} {t(unit)}")


@router.get("/drafts")
async def drafts(request: Request):
    chats = {c["chat_id"]: c for c in await db.list_chats()}
    approval = await db.get_state_bool("approval_mode")
    now = time.time()
    rows = await db.list_open_pending()
    for d in rows:
        chat = chats.get(d["chat_id"], {})
        rel = chat.get("relationship") or ""
        d["name"] = d["contact_name"] or (display_name(chat) if chat else str(d["chat_id"]))
        d["rel"] = rel
        d["left"] = _left(d["expires_at"] - now)
        # ponytail: no reason column is stored, so infer it from the current settings and the contact's group.
        if approval:
            d["reason"] = t("You asked to approve every reply first")
        elif rel in ("gf", "family", "bff", "close_friend"):
            d["reason"] = t("Sensitive message from {rel}").replace("{rel}", t(RELATIONSHIPS[rel]))
        else:
            d["reason"] = t("Held for your OK")
    return render(request, "drafts.html", drafts=rows)


@router.post("/drafts/{pid}")
async def resolve(request: Request, pid: int, action: str = Form(""), text: str = Form("")):
    bot = request.app.state.bot
    if action == "skip":
        result = await commands._resolve_pending(bot, pid, "skipped")
    elif action == "send":
        pending = await db.get_pending(pid)
        if pending is None:
            return back("/drafts", err="This reply was already handled or has expired.")
        body = text.replace("\r\n", "\n").strip()
        if not body:
            return back("/drafts", err="The reply is empty.")
        edited = body != pending["draft"].strip()
        # claim_pending inside _resolve_pending makes a double submit a no-op.
        result = await commands._resolve_pending(
            bot, pid, "edited" if edited else "approved", body if edited else None)
    else:
        return back("/drafts", err="Unknown action.")
    # _resolve_pending returns English lines for the Telegram owner; map them to fixed texts.
    if result.startswith("sent"):
        return back("/drafts", msg="Sent.")
    if result.startswith("skipped"):
        return back("/drafts", msg="Skipped. Nothing was sent.")
    if result.startswith("send failed"):
        return back("/drafts", err=t("Couldn't send it. Try again in a moment.") + result[len("send failed"):])
    return back("/drafts", err="This reply was already handled or has expired.")
