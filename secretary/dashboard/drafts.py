"""HITL drafts: send, edit-and-send, or skip pending replies from the browser."""
from __future__ import annotations

from fastapi import APIRouter, Form, Request

from .. import commands, db
from .web import back, render, t

router = APIRouter()


@router.get("/drafts")
async def drafts(request: Request):
    return render(request, "drafts.html", drafts=await db.list_open_pending())


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
