"""Contacts (tags, notes, prompt files, memory) and the Prompts editor."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Form, HTTPException, Request

from .. import commands, db, prompts
from ..config import settings
from .web import back, render, t

router = APIRouter()

# Human labels (English keys, translated by |t). Order = order shown in selects.
RELATIONSHIPS = {
    "gf": "Partner", "family": "Family", "bff": "Best friend", "close_friend": "Close friend",
    "friend": "Friend", "work": "Work", "acquaintance": "Acquaintance", "unknown": "Not set",
}
assert set(RELATIONSHIPS) == commands.VALID_RELATIONSHIPS
MEMORY_KINDS = {
    "fact": "Fact", "preference": "Likes and dislikes", "event": "Event", "promise": "Promise",
    "inside_joke": "Inside joke", "open_thread": "Unfinished topic",
}
NO_CONNECTION = "No active business connection yet. Connect the bot in Telegram first."
# One plain line per group: what choosing it changes. Translated by |t.
REL_DESC = {
    "gf": "Warm and close. Sensitive messages come to you first.",
    "family": "Respectful and warm.",
    "bff": "Very casual, with jokes.",
    "close_friend": "Casual and close.",
    "friend": "Friendly and short.",
    "work": "Polite and formal, makes no promises.",
    "acquaintance": "Polite and short.",
    "unknown": "Not sure yet, so it answers carefully and briefly.",
}
REL_HUE = {"gf": 350, "family": 40, "bff": 300, "close_friend": 260, "friend": 200, "work": 150, "acquaintance": 90}
CLOSE = ("gf", "family", "bff", "close_friend")
# Canned sample replies for the live preview: REPLIES[tone][len] + EMO[emoji].
REPLIES = [
    ["سلام، فردا عصر در دسترس نیستم. بعداً خبر می\u200cدهم.",
     "سلام، وقت بخیر. فردا تا ساعت شش جلسه دارم، ولی از هفت به بعد در خدمتم. اگر مناسب است همان موقع هماهنگ کنیم."],
    ["سلام! فردا تا شش سرم شلوغه، بعدش آزادم.",
     "سلام! فردا تا شش سر کارم، ولی از هفت به بعد آزادم. اگه برات خوبه همون موقع ببینیم، خبرم کن."],
    ["سلام! فردا تا شش گیرم، بعدش پایه\u200cام",
     "سلااام! فردا تا شش گیرم ولی از هفت به بعد کاملاً آزادم. بگو کجا بریم، من پایه\u200cام"],
]
EMO = ["", " 🙂", " 😄✨"]
PREVIEWS = {f"{t}-{n}-{e}": REPLIES[t][n] + EMO[e] for t in range(3) for n in range(2) for e in range(3)}
NEVER_CHOICES = (
    ("promise", "Make promises", "handshake"), ("meet", "Fix a firm meeting time", "event"),
    ("money", "Talk about money", "payments"), ("private", "Share my personal info", "lock"),
)


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


def _group_of(r: dict[str, Any]) -> str:
    return r.get("relationship") or "unknown"


@router.get("/contacts")
async def contacts(request: Request, q: str = "", filter: str = "all"):
    rows = await db.list_chats()
    for r in rows:
        r["name"] = display_name(r)
        r["rel"] = _group_of(r)
        r["rel_label"] = RELATIONSHIPS.get(r["rel"], "Not set")
    filters = {
        "all": lambda r: True,
        "unknown": lambda r: r["rel"] == "unknown",
        "close": lambda r: r["rel"] in CLOSE,
        "work": lambda r: r["rel"] == "work",
        "paused": lambda r: bool(r.get("paused")),
    }
    counts = {k: sum(1 for r in rows if f(r)) for k, f in filters.items()}
    filter = filter if filter in filters else "all"
    shown = [r for r in rows if filters[filter](r)]
    needle = q.strip().lower()
    if needle:
        shown = [r for r in shown
                 if needle in f"{r['chat_id']} {r['name']} {r.get('tg_username') or ''}".lower()]
    return render(request, "contacts.html", rows=shown, q=q, total=len(rows), filter=filter, counts=counts,
                  relationships=RELATIONSHIPS, rel_desc=REL_DESC, hues=REL_HUE)


@router.post("/contacts/{chat_id}/quick")
async def quick_profile(chat_id: int, relationship: str = Form(""), paused: str = Form("")):
    """Group + per-contact pause from the bottom sheet; leaves nickname and notes alone."""
    conn_id = await commands._active_conn_id()
    if conn_id is None:
        return back("/contacts", err=NO_CONNECTION)
    if relationship not in commands.VALID_RELATIONSHIPS:
        return back("/contacts", err="Unknown relationship.")
    await db.upsert_override(conn_id=conn_id, chat_id=chat_id, relationship=relationship, paused=paused == "on")
    prompts.clear_cache()
    return back("/contacts", msg="Saved.")


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
        relationships=RELATIONSHIPS, rel_desc=REL_DESC, hues=REL_HUE,
        prompt_text=read_text(_contact_file(chat_id)) or "",
        memories=await db.list_memory(conn_id=conn_id, chat_id=chat_id),
        kinds=MEMORY_KINDS,
        style=_style_rows(override.get("style_fingerprint")),
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
    return back(f"/contacts/{chat_id}", msg="Instructions saved.")


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
                msg="Reading the recent messages now. New facts show up here in about a minute (nothing changes if there's little new).")


PROMPT_NAMES = ("about_me", *RELATIONSHIPS)
LANGUAGES = {"en": "English", "fa": "Persian"}


def _style_rows(raw: str | None) -> list[tuple[str, Any]]:
    """The style fingerprint JSON as (label, value) rows; values stay raw for |t in the template."""
    try:
        fp = json.loads(raw or "")
    except ValueError:
        return []
    if not isinstance(fp, dict):
        return []
    rows = [
        ("Usual message length", fp.get("avg_length") or ""),
        ("Tone", fp.get("formality") or ""),
        ("Emoji", fp.get("emoji_freq") or ""),
        ("Languages", [LANGUAGES.get(x, x) for x in fp.get("languages") or []]),
        ("Pet names", fp.get("pet_names") or []),
        ("Usual opening", fp.get("signature_open") or ""),
        ("Usual closing", fp.get("signature_close") or ""),
    ]
    return [(label, v) for label, v in rows if v]


def _prompt_paths(name: str) -> tuple[Path, Path]:
    """(your file, shipped example) for a whitelisted prompt name."""
    folder = settings.prompts_dir if name == "about_me" else settings.prompts_dir / "personas"
    return folder / f"{name}.txt", folder / f"{name}.example.txt"


def _prompt_item(name: str) -> dict[str, str]:
    real, example = _prompt_paths(name)
    text, source = read_text(real), "Your own text"
    # The loader falls back to persona examples but never to about_me.example.txt,
    # so pre-filling that template would let one Save inject it into every prompt.
    if text is None and name != "about_me":
        text, source = read_text(example), "Ready-made sample. Saving makes it yours."
    if text is None and name in prompts.PERSONAS:
        text, source = prompts.PERSONAS[name], "Built-in default. Saving makes it yours."
    return {"name": name, "label": RELATIONSHIPS.get(name, "About me"),
            "text": text or "", "source": source if text else "Empty"}


@router.get("/prompts")
async def prompts_page(request: Request, g: str = "gf"):
    g = g if g in RELATIONSHIPS else "gf"
    people = sum(1 for r in await db.list_chats() if _group_of(r) == g)
    tone = prompts.tone_for(g)
    return render(
        request, "prompts.html",
        about=_prompt_item("about_me"), item=_prompt_item(g), g=g, people=people,
        relationships=RELATIONSHIPS, rel_desc=REL_DESC, hues=REL_HUE,
        tone=tone, customized=g in prompts.load_tone(), previews=PREVIEWS,
        never=prompts.load_tone().get("never", []), never_choices=NEVER_CHOICES,
    )


@router.post("/prompts/{name}")
async def save_prompt(name: str, text: str = Form(""), g: str = Form("")):
    if name not in PROMPT_NAMES:  # whitelist: user input never forms a path
        raise HTTPException(status_code=404)
    write_prompt(_prompt_paths(name)[0], text)
    return back("/prompts", msg=t("Saved:") + " " + t(RELATIONSHIPS.get(name, "About me")),
                **({"g": g} if g in RELATIONSHIPS else {}))


# Declared before /tone/{g} so "never" is never read as a group.
@router.post("/tone/never")
async def save_never(never: list[str] = Form([])):
    try:
        prompts.save_tone({**prompts.load_tone(), "never": never})
    except ValueError:
        return back("/prompts", err="Couldn't save that choice.")
    return back("/prompts", msg="Saved.")


@router.post("/tone/{g}")
async def save_group_tone(g: str, tone: str = Form(""), len: str = Form(""), emoji: str = Form("")):
    if g not in RELATIONSHIPS:
        raise HTTPException(status_code=404)
    try:
        axes = {"tone": int(tone), "len": int(len), "emoji": int(emoji)}
        prompts.save_tone({**prompts.load_tone(), g: axes})
    except ValueError:
        return back("/prompts", err="Couldn't save that choice.", g=g)
    return back("/prompts", msg="Saved.", g=g)


@router.post("/tone/{g}/reset")
async def reset_group_tone(g: str):
    if g not in RELATIONSHIPS:
        raise HTTPException(status_code=404)
    data = prompts.load_tone()
    data.pop(g, None)
    prompts.save_tone(data)
    return back("/prompts", msg="Back to the suggested style.", g=g)
