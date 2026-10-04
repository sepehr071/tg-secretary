"""Contacts (tags, notes, prompt files, memory) and the Prompts editor."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Form, HTTPException, Request

from .. import commands, db, prompts
from ..config import settings
from .web import back, render

router = APIRouter()

MEMORY_KINDS = ("fact", "preference", "event", "promise", "inside_joke", "open_thread")
NO_CONNECTION = "No active business connection yet. Connect the bot in Telegram first."


def display_name(row: dict[str, Any]) -> str:
    full = " ".join(p for p in (row.get("tg_first_name"), row.get("tg_last_name")) if p)
    username = f"@{row['tg_username']}" if row.get("tg_username") else ""
    return row.get("nickname") or full or username or str(row["chat_id"])


def read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def write_prompt(path: Path, text: str) -> None:
    """Save textarea content as UTF-8 with LF endings; empty text deletes the file."""
    text = text.replace("\r\n", "\n").strip()
    if text:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text + "\n", encoding="utf-8", newline="\n")
    else:
        path.unlink(missing_ok=True)
    prompts.clear_cache()


def _contact_file(chat_id: int) -> Path:
    return settings.prompts_dir / "contacts" / f"{chat_id}.txt"


@router.get("/contacts")
async def contacts(request: Request, q: str = ""):
    rows = await db.list_chats()
    for r in rows:
        r["name"] = display_name(r)
    needle = q.strip().lower()
    if needle:
        rows = [r for r in rows
                if needle in f"{r['chat_id']} {r['name']} {r.get('tg_username') or ''}".lower()]
    return render(request, "contacts.html", rows=rows, q=q)


@router.get("/contacts/{chat_id}")
async def contact(request: Request, chat_id: int):
    conn_id = await commands._active_conn_id()
    if conn_id is None:
        return back("/contacts", err=NO_CONNECTION)
    override = await db.get_override(conn_id=conn_id, chat_id=chat_id) or {}
    return render(
        request, "contact.html",
        chat_id=chat_id,
        title=display_name({**override, "chat_id": chat_id}),
        o=override,
        relationships=sorted(commands.VALID_RELATIONSHIPS),
        prompt_text=read_text(_contact_file(chat_id)) or "",
        memories=await db.list_memory(conn_id=conn_id, chat_id=chat_id),
        kinds=MEMORY_KINDS,
        history=await db.load_history(conn_id=conn_id, chat_id=chat_id, limit=20),
    )


@router.post("/contacts/{chat_id}/profile")
async def save_profile(
    chat_id: int,
    relationship: str = Form(""),
    nickname: str = Form(""),
    persona_extra: str = Form(""),
    paused: str = Form(""),
):
    conn_id = await commands._active_conn_id()
    if conn_id is None:
        return back("/contacts", err=NO_CONNECTION)
    if relationship not in commands.VALID_RELATIONSHIPS:
        return back(f"/contacts/{chat_id}", err="Unknown relationship.")
    await db.upsert_override(
        conn_id=conn_id, chat_id=chat_id,
        relationship=relationship,
        nickname=nickname.strip(),
        persona_extra=persona_extra.replace("\r\n", "\n").strip(),
        paused=paused == "on",
    )
    prompts.clear_cache()
    return back(f"/contacts/{chat_id}", msg="Saved.")


@router.post("/contacts/{chat_id}/prompt")
async def save_contact_prompt(chat_id: int, text: str = Form("")):
    write_prompt(_contact_file(chat_id), text)
    return back(f"/contacts/{chat_id}", msg="Prompt file saved.")


@router.post("/contacts/{chat_id}/memory")
async def add_memory(chat_id: int, kind: str = Form(""), content: str = Form("")):
    conn_id = await commands._active_conn_id()
    if conn_id is None:
        return back("/contacts", err=NO_CONNECTION)
    if kind not in MEMORY_KINDS or not content.strip():
        return back(f"/contacts/{chat_id}", err="Pick a kind and write the memory.")
    await db.add_memory(conn_id=conn_id, chat_id=chat_id, kind=kind, content=content.strip())
    return back(f"/contacts/{chat_id}", msg="Memory added.")


@router.post("/contacts/{chat_id}/memory/{memory_id}/expire")
async def expire_memory(chat_id: int, memory_id: int):
    await db.expire_memory(memory_id)
    return back(f"/contacts/{chat_id}", msg="Memory removed.")


@router.post("/contacts/{chat_id}/extract")
async def extract(chat_id: int):
    conn_id = await commands._active_conn_id()
    if conn_id is None:
        return back("/contacts", err=NO_CONNECTION)
    await db.enqueue(conn_id=conn_id, chat_id=chat_id)
    return back(f"/contacts/{chat_id}",
                msg="Extraction queued. The memory worker runs it within ~15s (it skips chats with few new messages).")


PROMPT_NAMES = ("about_me", *sorted(commands.VALID_RELATIONSHIPS))


def _prompt_paths(name: str) -> tuple[Path, Path]:
    """(your file, shipped example) for a whitelisted prompt name."""
    folder = settings.prompts_dir if name == "about_me" else settings.prompts_dir / "personas"
    return folder / f"{name}.txt", folder / f"{name}.example.txt"


@router.get("/prompts")
async def prompts_page(request: Request):
    items = []
    for name in PROMPT_NAMES:
        real, example = _prompt_paths(name)
        text, source = read_text(real), "your file"
        # The loader falls back to persona examples but never to about_me.example.txt,
        # so pre-filling that template would let one Save inject it into every prompt.
        if text is None and name != "about_me":
            text, source = read_text(example), "example — saving creates your own file"
        if text is None and name in prompts.PERSONAS:
            text, source = prompts.PERSONAS[name], "built-in default — saving creates your own file"
        items.append({"name": name, "text": text or "", "source": source if text else "empty"})
    return render(request, "prompts.html", items=items)


@router.post("/prompts/{name}")
async def save_prompt(name: str, text: str = Form("")):
    if name not in PROMPT_NAMES:  # whitelist: user input never forms a path
        raise HTTPException(status_code=404)
    write_prompt(_prompt_paths(name)[0], text)
    return back("/prompts", msg=f"Saved {name}.")
