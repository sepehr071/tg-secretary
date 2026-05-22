import logging
import shutil
import time
from pathlib import Path
from typing import Any

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from . import db, llm, memory, prompts
from .config import settings
from .prompts import load_system_prompt

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _is_owner(update: Update) -> bool:
    return (
        update.effective_user is not None
        and update.effective_user.id == settings.owner_user_id
    )


# Telegram caps a single sendMessage at 4096 chars. Stay under it with margin.
_TG_MSG_LIMIT = 3800


def _split_for_tg(text: str, limit: int = _TG_MSG_LIMIT) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    buf = ""
    for line in text.split("\n"):
        if len(line) > limit:
            if buf:
                chunks.append(buf)
                buf = ""
            for i in range(0, len(line), limit):
                chunks.append(line[i:i + limit])
            continue
        candidate = line if not buf else buf + "\n" + line
        if len(candidate) > limit:
            chunks.append(buf)
            buf = line
        else:
            buf = candidate
    if buf:
        chunks.append(buf)
    return chunks


async def _reply(update: Update, text: str) -> None:
    if update.effective_message is None:
        return
    for chunk in _split_for_tg(text):
        await update.effective_message.reply_text(chunk)


async def _active_conn_id() -> str | None:
    conn = db._db()
    cur = await conn.execute(
        "SELECT conn_id FROM connections WHERE is_enabled = 1 ORDER BY updated_at DESC LIMIT 1"
    )
    row = await cur.fetchone()
    if row is None:
        return None
    return row["conn_id"]


def _parse_int(s: str) -> int | None:
    try:
        return int(s)
    except (TypeError, ValueError):
        return None


def _strip_quotes(s: str) -> str:
    s = s.strip()
    if len(s) >= 2 and s[0] in '"“«\'' and s[-1] in '"”»\'':
        return s[1:-1].strip()
    return s


VALID_RELATIONSHIPS = {
    "gf", "bff", "close_friend", "friend", "family", "work", "acquaintance", "unknown",
}


# ---------------------------------------------------------------------------
# state commands
# ---------------------------------------------------------------------------


async def on_pause(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """No arg → global pause. With chat_id → per-chat pause."""
    if not _is_owner(update):
        return
    args = ctx.args or []
    if not args:
        await db.set_state_bool("paused", True)
        await _reply(update, "🛑 Bot paused globally.")
        return
    chat_id = _parse_int(args[0])
    if chat_id is None:
        await _reply(update, "usage: /pause [chat_id]")
        return
    conn_id = await _active_conn_id()
    if conn_id is None:
        await _reply(update, "no active business connection")
        return
    await db.set_paused(conn_id=conn_id, chat_id=chat_id, paused=True)
    await _reply(update, f"🛑 paused chat {chat_id}")


async def on_resume(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """No arg → global resume. With chat_id → per-chat resume."""
    if not _is_owner(update):
        return
    args = ctx.args or []
    if not args:
        await db.set_state_bool("paused", False)
        await _reply(update, "▶️ Bot resumed.")
        return
    chat_id = _parse_int(args[0])
    if chat_id is None:
        await _reply(update, "usage: /resume [chat_id]")
        return
    conn_id = await _active_conn_id()
    if conn_id is None:
        await _reply(update, "no active business connection")
        return
    await db.set_paused(conn_id=conn_id, chat_id=chat_id, paused=False)
    await _reply(update, f"▶️ resumed chat {chat_id}")


async def on_status(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_owner(update):
        return
    paused = await db.get_state_bool("paused", False)
    approval = await db.get_state_bool("approval_mode", False)
    innercircle = await db.get_state_bool("innercircle_gate", False)
    qstart = await db.get_state("quiet_start") or "—"
    qend = await db.get_state("quiet_end") or "—"

    conn = db._db()
    cur = await conn.execute(
        "SELECT COUNT(*) AS n FROM pending_replies WHERE status = 'pending'"
    )
    row = await cur.fetchone()
    pending_count = row["n"] if row else 0

    cur = await conn.execute(
        """
        SELECT chat_id, content, created_at FROM messages
        WHERE role = 'assistant' AND via_bot = 1
        ORDER BY id DESC LIMIT 5
        """
    )
    last_rows = await cur.fetchall()
    last_lines = []
    for r in last_rows:
        preview = r["content"][:60].replace("\n", " ")
        last_lines.append(f"  • chat {r['chat_id']}: {preview}")
    last_block = "\n".join(last_lines) if last_lines else "  (none)"

    text = (
        f"paused: {paused}\n"
        f"approval_mode: {approval}\n"
        f"innercircle_gate: {innercircle}\n"
        f"quiet_hours: {qstart} → {qend}\n"
        f"model: {settings.openrouter_model}\n"
        f"pending_replies: {pending_count}\n"
        f"last 5 bot replies:\n{last_block}"
    )
    await _reply(update, text)


async def on_stats(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_owner(update):
        return
    conn = db._db()
    now = int(time.time())
    day_ago = now - 86400
    week_ago = now - 7 * 86400

    def _count(row) -> int:
        return int(row["n"]) if row else 0

    cur = await conn.execute("SELECT COUNT(DISTINCT chat_id) AS n FROM messages")
    total_chats = _count(await cur.fetchone())

    cur = await conn.execute(
        "SELECT COUNT(*) AS n FROM messages WHERE role='assistant' AND via_bot=1 AND created_at >= ?",
        (day_ago,),
    )
    replies_today = _count(await cur.fetchone())

    cur = await conn.execute(
        "SELECT COUNT(*) AS n FROM messages WHERE role='assistant' AND via_bot=1 AND created_at >= ?",
        (week_ago,),
    )
    replies_week = _count(await cur.fetchone())

    cur = await conn.execute(
        """
        SELECT COUNT(*) AS n FROM messages u
        WHERE u.role = 'user'
          AND NOT EXISTS (
            SELECT 1 FROM messages a
            WHERE a.conn_id = u.conn_id AND a.chat_id = u.chat_id
              AND a.role = 'assistant' AND a.via_bot = 1
              AND a.created_at BETWEEN u.created_at AND u.created_at + 300
          )
        """
    )
    aborted = _count(await cur.fetchone())

    text = (
        f"total chats: {total_chats}\n"
        f"replies today: {replies_today}\n"
        f"replies this week: {replies_week}\n"
        f"aborted (approx, no reply within 5min): {aborted}"
    )
    await _reply(update, text)


async def on_last(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_owner(update):
        return
    n = 5
    if ctx.args:
        parsed = _parse_int(ctx.args[0])
        if parsed is not None and parsed > 0:
            n = min(parsed, 50)
    conn = db._db()
    cur = await conn.execute(
        """
        SELECT chat_id, content FROM messages
        WHERE role = 'assistant' AND via_bot = 1
        ORDER BY id DESC LIMIT ?
        """,
        (n,),
    )
    rows = await cur.fetchall()
    if not rows:
        await _reply(update, "(no bot replies yet)")
        return
    lines = []
    for r in rows:
        preview = r["content"][:80].replace("\n", " ")
        lines.append(f"chat {r['chat_id']}: {preview}")
    await _reply(update, "\n".join(lines))


# ---------------------------------------------------------------------------
# per-chat commands
# ---------------------------------------------------------------------------


async def on_note(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Set the persona_extra note for this chat. Overwrites previous note."""
    if not _is_owner(update):
        return
    if len(ctx.args) < 2:
        await _reply(update, "usage: /note <chat_id> <text>")
        return
    chat_id = _parse_int(ctx.args[0])
    if chat_id is None:
        await _reply(update, "invalid chat_id")
        return
    text = _strip_quotes(" ".join(ctx.args[1:]))
    conn_id = await _active_conn_id()
    if conn_id is None:
        await _reply(update, "no active business connection")
        return
    await db.set_persona_extra(conn_id=conn_id, chat_id=chat_id, text=text)
    await _reply(update, f"note set for chat {chat_id}")


async def on_prompt(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Add a fact / instruction about a contact. Stacks (one row per call).
    Examples:
        /prompt 4838282 hanie loves dc comics
        /prompt 4838282 "always reply to her in Persian"
    Stored in contact_memory with kind=fact; rendered in the system prompt under
    "What you know about them".
    """
    if not _is_owner(update):
        return
    args = ctx.args or []
    if len(args) < 2:
        await _reply(
            update,
            'usage: /prompt <chat_id> <fact about them>\n'
            'e.g.  /prompt 4838282 hanie loves dc comics',
        )
        return
    chat_id = _parse_int(args[0])
    if chat_id is None:
        await _reply(update, "invalid chat_id")
        return
    text = _strip_quotes(" ".join(args[1:]))
    if not text:
        await _reply(update, "empty fact")
        return
    conn_id = await _active_conn_id()
    if conn_id is None:
        await _reply(update, "no active business connection")
        return
    mem_id = await db.add_memory(
        conn_id=conn_id, chat_id=chat_id, kind="fact", content=text
    )
    await _reply(update, f"✅ saved #{mem_id} for chat {chat_id}: {text}")


async def on_preview(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_owner(update):
        return
    if not ctx.args:
        await _reply(update, "usage: /preview <text>")
        return
    user_message = " ".join(ctx.args)
    try:
        draft = await llm.generate_reply(
            system_prompt=load_system_prompt(),
            history=[],
            user_message=user_message,
        )
    except Exception as e:  # noqa: BLE001
        await _reply(update, f"LLM error: {e}")
        return
    await _reply(update, f"draft:\n{draft}")


async def on_approval(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_owner(update):
        return
    if not ctx.args or ctx.args[0].lower() not in ("on", "off"):
        await _reply(update, "usage: /approval on|off")
        return
    val = ctx.args[0].lower() == "on"
    await db.set_state_bool("approval_mode", val)
    await _reply(update, f"approval_mode: {val}")


async def on_quiet(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_owner(update):
        return
    if not ctx.args:
        await _reply(update, "usage: /quiet HH:MM HH:MM | /quiet off")
        return
    if ctx.args[0].lower() == "off":
        await db.set_state("quiet_start", "")
        await db.set_state("quiet_end", "")
        await _reply(update, "quiet hours cleared")
        return
    if len(ctx.args) < 2:
        await _reply(update, "usage: /quiet HH:MM HH:MM")
        return
    start, end = ctx.args[0], ctx.args[1]
    for t in (start, end):
        parts = t.split(":")
        if len(parts) != 2 or _parse_int(parts[0]) is None or _parse_int(parts[1]) is None:
            await _reply(update, f"invalid time: {t}")
            return
    await db.set_state("quiet_start", start)
    await db.set_state("quiet_end", end)
    await _reply(update, f"quiet hours: {start} → {end}")


async def on_who(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_owner(update):
        return
    if len(ctx.args) < 2:
        await _reply(update, "usage: /who <chat_id> <relationship> [nickname]")
        return
    chat_id = _parse_int(ctx.args[0])
    if chat_id is None:
        await _reply(update, "invalid chat_id")
        return
    relationship = ctx.args[1].lower()
    if relationship not in VALID_RELATIONSHIPS:
        await _reply(update, f"relationship must be one of: {', '.join(sorted(VALID_RELATIONSHIPS))}")
        return
    nickname = " ".join(ctx.args[2:]) if len(ctx.args) > 2 else None
    conn_id = await _active_conn_id()
    if conn_id is None:
        await _reply(update, "no active business connection")
        return
    await db.set_relationship(
        conn_id=conn_id, chat_id=chat_id, relationship=relationship, nickname=nickname
    )
    await _reply(update, f"chat {chat_id}: relationship={relationship} nickname={nickname or '—'}")


async def on_memory(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_owner(update):
        return
    if not ctx.args:
        await _reply(update, "usage: /memory <chat_id>")
        return
    chat_id = _parse_int(ctx.args[0])
    if chat_id is None:
        await _reply(update, "invalid chat_id")
        return
    conn_id = await _active_conn_id()
    if conn_id is None:
        await _reply(update, "no active business connection")
        return
    rows = await db.list_memory(conn_id=conn_id, chat_id=chat_id)
    if not rows:
        await _reply(update, "(no memory)")
        return
    lines = [f"#{r['id']} [{r['kind']}] {r['content']}" for r in rows]
    await _reply(update, "\n".join(lines))


async def on_forget(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_owner(update):
        return
    if not ctx.args:
        await _reply(update, "usage: /forget <memory_id>")
        return
    mem_id = _parse_int(ctx.args[0])
    if mem_id is None:
        await _reply(update, "invalid memory_id")
        return
    await db.expire_memory(mem_id)
    await _reply(update, f"forgot memory #{mem_id}")


async def on_innercircle(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_owner(update):
        return
    if not ctx.args or ctx.args[0].lower() not in ("on", "off"):
        await _reply(update, "usage: /innercircle on|off")
        return
    val = ctx.args[0].lower() == "on"
    await db.set_state_bool("innercircle_gate", val)
    await _reply(update, f"innercircle_gate: {val}")


async def on_persona(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Show resolved system prompt + path to the per-contact .txt file."""
    if not _is_owner(update):
        return
    if not ctx.args:
        await _reply(update, "usage: /persona <chat_id>")
        return
    chat_id = _parse_int(ctx.args[0])
    if chat_id is None:
        await _reply(update, "invalid chat_id")
        return
    prompts_dir = getattr(settings, "prompts_dir", None) or Path("./prompts")
    contact_path = Path(prompts_dir).absolute() / "contacts" / f"{chat_id}.txt"
    prompt = load_system_prompt(chat_id=chat_id)
    if len(prompt) > 3500:
        prompt = prompt[:3500] + "\n…[truncated]"
    await _reply(
        update,
        f"📄 per-contact file: {contact_path}\n\n--- resolved prompt ---\n{prompt}",
    )


# ---------------------------------------------------------------------------
# pending-reply approval flow (regex handlers)
# ---------------------------------------------------------------------------


def _extract_pending_id(text: str) -> int | None:
    head = text.split(None, 1)[0]
    _, _, num = head.partition("_")
    return _parse_int(num)


async def _record_sent(pending: dict[str, Any], text: str, sent_msg_id: int | None) -> None:
    await db.append_message(
        conn_id=pending["conn_id"],
        chat_id=pending["chat_id"],
        role="assistant",
        content=text,
        tg_message_id=sent_msg_id,
        via_bot=True,
    )


async def on_approve(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_owner(update):
        return
    msg = update.effective_message
    if msg is None or not msg.text:
        return
    pid = _extract_pending_id(msg.text)
    if pid is None:
        return
    pending = await db.get_pending(pid)
    if pending is None:
        await _reply(update, f"pending #{pid} not found")
        return
    if pending["status"] != "pending":
        await _reply(update, f"pending #{pid} already {pending['status']}")
        return
    try:
        sent = await ctx.bot.send_message(
            chat_id=pending["chat_id"],
            text=pending["draft"],
            business_connection_id=pending["conn_id"],
        )
    except Exception as e:  # noqa: BLE001
        await _reply(update, f"send failed: {e}")
        return
    await db.set_pending_status(pid, "approved")
    await _record_sent(pending, pending["draft"], sent.message_id)
    await _reply(update, f"sent pending #{pid}")


async def on_edit(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_owner(update):
        return
    msg = update.effective_message
    if msg is None or not msg.text:
        return
    parts = msg.text.split(None, 1)
    if len(parts) < 2 or not parts[1].strip():
        await _reply(update, "usage: /edit_<id> <new text>")
        return
    pid = _extract_pending_id(msg.text)
    if pid is None:
        return
    new_text = parts[1].strip()
    pending = await db.get_pending(pid)
    if pending is None:
        await _reply(update, f"pending #{pid} not found")
        return
    if pending["status"] != "pending":
        await _reply(update, f"pending #{pid} already {pending['status']}")
        return
    try:
        sent = await ctx.bot.send_message(
            chat_id=pending["chat_id"],
            text=new_text,
            business_connection_id=pending["conn_id"],
        )
    except Exception as e:  # noqa: BLE001
        await _reply(update, f"send failed: {e}")
        return
    await db.set_pending_status(pid, "edited")
    await _record_sent(pending, new_text, sent.message_id)
    await _reply(update, f"sent edited pending #{pid}")


async def on_skip(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_owner(update):
        return
    msg = update.effective_message
    if msg is None or not msg.text:
        return
    pid = _extract_pending_id(msg.text)
    if pid is None:
        return
    pending = await db.get_pending(pid)
    if pending is None:
        await _reply(update, f"pending #{pid} not found")
        return
    await db.set_pending_status(pid, "skipped")
    await _reply(update, f"skipped pending #{pid}")


# ---------------------------------------------------------------------------
# /help
# ---------------------------------------------------------------------------


HELP_TEXT = """\
Owner commands:

State:
 /pause [chat_id] · /resume [chat_id] — no arg = global, with id = per-chat
 /status — current settings + counts
 /stats — reply counts (today / week)
 /last [N] — last N bot replies

Contacts:
 /who <chat_id> <relationship> [nickname]
 /contacts — list tagged contacts
 /senders [N] — recent inbound chats (incl. untagged)
 /find <query> — search nicknames + names

Memory (what the bot knows about a person):
 /profile <chat_id> [refresh] — show / rebuild the narrative briefing on this person
 /prompt <chat_id> <fact> — add a fact (stacks). e.g. /prompt 123 "hanie loves dc comics"
 /note <chat_id> <text> — set a free-form persona note (overwrites)
 /memory <chat_id> — list stored memory rows (raw facts feeding the profile)
 /forget <memory_id> — drop one memory row
 /extract <chat_id> — force memory extraction now
 /style <chat_id> — show owner-style fingerprint
 /purge <chat_id> | all — wipe memory for that chat (rebuilds from scratch)
 /wipe <chat_id> — purge ALL data for chat (messages + memory + summary + profile)

Persona files:
 /persona <chat_id> — show resolved system prompt + file path
 /reload — re-read all .txt prompts from disk

Modes:
 /approval on|off — HITL gate
 /innercircle on|off — gf/family/bff safety gate
 /voice on|off — voice transcription toggle
 /quiet HH:MM HH:MM | off — quiet hours

Tuning (live, no restart):
 /delay [seconds] — auto-reply delay (default 30)
 /away_delay [seconds] — short delay in away mode (default 5)
 /cooldown [seconds] — owner-active mute window (default 600)

Tools:
 /preview <text> — dry-run draft
 /say <chat_id> <text> — send manually as the bot
 /pending — outstanding HITL drafts
 /backup — copy secretary.db to a timestamped file

HITL replies (in pending DM):
 /approve_<id>  /edit_<id> <text>  /skip_<id>
"""


async def on_help(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_owner(update):
        return
    await _reply(update, HELP_TEXT)


# ---------------------------------------------------------------------------
# contacts / discovery
# ---------------------------------------------------------------------------


async def on_senders(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_owner(update):
        return
    n = 20
    if ctx.args:
        parsed = _parse_int(ctx.args[0])
        if parsed is not None and parsed > 0:
            n = min(parsed, 100)
    conn = db._db()
    cur = await conn.execute(
        """
        SELECT
            m.chat_id,
            COUNT(*) AS msg_count,
            MAX(m.created_at) AS last_seen,
            o.relationship,
            o.nickname,
            o.tg_first_name,
            o.tg_last_name,
            o.tg_username
        FROM messages m
        LEFT JOIN contact_overrides o ON o.chat_id = m.chat_id AND o.conn_id = m.conn_id
        WHERE m.role = 'user'
        GROUP BY m.chat_id
        ORDER BY last_seen DESC
        LIMIT ?
        """,
        (n,),
    )
    rows = list(await cur.fetchall())
    if not rows:
        await _reply(update, "(no inbound messages yet)")
        return
    now = int(time.time())
    lines = [f"last {len(rows)} senders (newest first):"]
    for r in rows:
        age_min = (now - r["last_seen"]) // 60
        rel = r["relationship"] or "untagged"
        nick = r["nickname"]
        full = " ".join(p for p in (r["tg_first_name"], r["tg_last_name"]) if p)
        uname = f"@{r['tg_username']}" if r["tg_username"] else ""
        who = nick or full or uname or "?"
        uname_tail = f" ({uname})" if uname and who != uname else ""
        lines.append(
            f"  {r['chat_id']}  [{rel}]  {who}{uname_tail}  · {r['msg_count']} msgs · {age_min}m ago"
        )
    lines.append("\ntag with: /who <chat_id> <relationship> [nickname]")
    await _reply(update, "\n".join(lines))


async def on_contacts(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_owner(update):
        return
    conn = db._db()
    cur = await conn.execute(
        """
        SELECT chat_id, relationship, nickname, paused, updated_at,
               tg_first_name, tg_last_name, tg_username
        FROM contact_overrides
        ORDER BY updated_at DESC
        """
    )
    rows = list(await cur.fetchall())
    if not rows:
        await _reply(update, "no tagged contacts yet. use /who <chat_id> <relationship> [nickname]")
        return
    lines = ["tagged contacts:"]
    for r in rows:
        pause_mark = " 🛑" if r["paused"] else ""
        nick = r["nickname"]
        full = " ".join(p for p in (r["tg_first_name"], r["tg_last_name"]) if p)
        uname = f"@{r['tg_username']}" if r["tg_username"] else ""
        who = nick or full or uname or "?"
        uname_tail = f" ({uname})" if uname and who != uname else ""
        lines.append(
            f"  {r['chat_id']}  [{r['relationship']}]  {who}{uname_tail}{pause_mark}"
        )
    await _reply(update, "\n".join(lines))


async def on_find(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_owner(update):
        return
    query = " ".join(ctx.args or []).strip().lower()
    if not query:
        await _reply(update, "usage: /find <query>")
        return
    conn = db._db()
    like = f"%{query}%"
    cur = await conn.execute(
        """
        SELECT chat_id, relationship, nickname, tg_first_name, tg_last_name, tg_username
        FROM contact_overrides
        WHERE LOWER(COALESCE(nickname,'')) LIKE ?
           OR LOWER(COALESCE(tg_first_name,'')) LIKE ?
           OR LOWER(COALESCE(tg_last_name,'')) LIKE ?
           OR LOWER(COALESCE(tg_username,'')) LIKE ?
        """,
        (like, like, like, like),
    )
    rows = list(await cur.fetchall())
    if not rows:
        await _reply(update, f"no contact matches '{query}'")
        return
    lines = [f"matches for '{query}':"]
    for r in rows:
        nick = r["nickname"]
        full = " ".join(p for p in (r["tg_first_name"], r["tg_last_name"]) if p)
        uname = f"@{r['tg_username']}" if r["tg_username"] else ""
        who = nick or full or uname or "?"
        uname_tail = f" ({uname})" if uname and who != uname else ""
        lines.append(f"  {r['chat_id']}  [{r['relationship']}]  {who}{uname_tail}")
    await _reply(update, "\n".join(lines))


async def on_say(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_owner(update):
        return
    args = ctx.args or []
    if len(args) < 2:
        await _reply(update, "usage: /say <chat_id> <text>")
        return
    chat_id = _parse_int(args[0])
    if chat_id is None:
        await _reply(update, "bad chat_id")
        return
    text = " ".join(args[1:]).strip()
    if not text:
        await _reply(update, "empty text")
        return
    conn_id = await _active_conn_id()
    if conn_id is None:
        await _reply(update, "no active business connection")
        return
    try:
        sent = await ctx.bot.send_message(
            chat_id=chat_id, text=text, business_connection_id=conn_id
        )
    except Exception as e:  # noqa: BLE001
        await _reply(update, f"send failed: {e}")
        return
    await db.append_message(
        conn_id=conn_id, chat_id=chat_id, role="assistant",
        content=text, tg_message_id=sent.message_id, via_bot=True,
    )
    await _reply(update, f"✅ sent to {chat_id} ({len(text)} chars)")


async def on_reload(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_owner(update):
        return
    cleared = prompts.clear_cache()
    await _reply(update, f"🔄 prompt cache cleared ({cleared} entries). next call re-reads .txt files.")


async def on_pending(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_owner(update):
        return
    conn = db._db()
    now = int(time.time())
    cur = await conn.execute(
        """
        SELECT id, chat_id, contact_name, contact_msg, draft, created_at, expires_at
        FROM pending_replies
        WHERE status = 'pending' AND expires_at > ?
        ORDER BY id DESC LIMIT 20
        """,
        (now,),
    )
    rows = list(await cur.fetchall())
    if not rows:
        await _reply(update, "no pending drafts")
        return
    lines = ["pending HITL drafts:"]
    for r in rows:
        age = now - r["created_at"]
        ttl = r["expires_at"] - now
        msg_prev = (r["contact_msg"] or "")[:60]
        draft_prev = (r["draft"] or "")[:60]
        name = r["contact_name"] or str(r["chat_id"])
        lines.append(
            f"  #{r['id']}  {name}  age={age}s ttl={ttl}s\n"
            f"    msg: {msg_prev!r}\n"
            f"    draft: {draft_prev!r}"
        )
    await _reply(update, "\n".join(lines))


# ----- tuning (live overrides via bot_state) -----


async def _tune_int(update: Update, ctx: ContextTypes.DEFAULT_TYPE, *, key: str, default: int, label: str) -> None:
    if not _is_owner(update):
        return
    args = ctx.args or []
    if not args:
        cur_val = await db.get_state(key)
        eff = int(cur_val) if cur_val else default
        await _reply(update, f"{label} = {eff}s (override: {cur_val or 'none'})")
        return
    if args[0].lower() in ("off", "default", "clear"):
        await db.set_state(key, "")
        await _reply(update, f"{label} reset to env default ({default}s)")
        return
    n = _parse_int(args[0])
    if n is None or n < 0:
        await _reply(update, "bad value (need non-negative integer)")
        return
    await db.set_state(key, str(n))
    await _reply(update, f"{label} = {n}s")


async def on_delay(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await _tune_int(
        update, ctx,
        key="delay_override", default=settings.auto_reply_delay_seconds,
        label="auto_reply_delay",
    )


async def on_away_delay(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await _tune_int(
        update, ctx,
        key="away_delay_override", default=settings.away_reply_delay_seconds,
        label="away_reply_delay",
    )


async def on_cooldown(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await _tune_int(
        update, ctx,
        key="cooldown_override", default=settings.owner_active_cooldown_seconds,
        label="owner_active_cooldown",
    )


async def on_voice(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_owner(update):
        return
    args = ctx.args or []
    if not args:
        cur_val = await db.get_state("voice_override")
        await _reply(update, f"voice_transcribe override = {cur_val or 'none (env default)'}")
        return
    val = args[0].lower()
    if val not in ("on", "off"):
        await _reply(update, "usage: /voice on|off")
        return
    await db.set_state("voice_override", "on" if val == "on" else "off")
    await _reply(update, f"🎙 voice transcription = {val}")


# ----- memory ops -----


async def on_extract(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_owner(update):
        return
    args = ctx.args or []
    if not args:
        await _reply(update, "usage: /extract <chat_id>")
        return
    chat_id = _parse_int(args[0])
    if chat_id is None:
        await _reply(update, "bad chat_id")
        return
    conn_id = await _active_conn_id()
    if conn_id is None:
        await _reply(update, "no active connection")
        return
    await memory._extract_for_chat(conn_id, chat_id)
    await _reply(update, f"✅ extraction run for chat {chat_id}. /memory {chat_id} to view.")


async def on_profile(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the synthesized per-contact narrative profile.
    /profile <chat_id>         — view current profile + age.
    /profile <chat_id> refresh — force regeneration now.
    """
    if not _is_owner(update):
        return
    args = ctx.args or []
    if not args:
        await _reply(update, "usage: /profile <chat_id> [refresh]")
        return
    chat_id = _parse_int(args[0])
    if chat_id is None:
        await _reply(update, "bad chat_id")
        return
    refresh = len(args) > 1 and args[1].lower() == "refresh"
    conn_id = await _active_conn_id()
    if conn_id is None:
        await _reply(update, "no active connection")
        return
    if refresh:
        await _reply(update, f"⏳ regenerating profile for chat {chat_id}…")
        new_profile = await memory.maybe_refresh_profile(conn_id, chat_id, force=True)
        if not new_profile:
            await _reply(update, "no profile generated (extractor empty or no memory rows)")
            return
        await _reply(update, f"✅ profile refreshed ({len(new_profile)} chars):\n\n{new_profile}")
        return
    override = await db.get_override(conn_id=conn_id, chat_id=chat_id)
    profile_text = override.get("profile") if override else None
    if not profile_text:
        await _reply(
            update,
            f"no profile for chat {chat_id} yet.\n"
            f"build one with: /profile {chat_id} refresh",
        )
        return
    updated_at = (override.get("profile_updated_at") if override else None) or 0
    mem_count = (override.get("profile_memory_count_at_update") if override else None) or 0
    age_days = (int(time.time()) - int(updated_at)) // 86400 if updated_at else None
    age_label = f"{age_days}d ago" if age_days is not None else "unknown"
    await _reply(
        update,
        f"📄 profile for {chat_id} · built from {mem_count} memories · {age_label}\n\n{profile_text}",
    )


async def on_style(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_owner(update):
        return
    args = ctx.args or []
    if not args:
        await _reply(update, "usage: /style <chat_id>")
        return
    chat_id = _parse_int(args[0])
    if chat_id is None:
        await _reply(update, "bad chat_id")
        return
    conn_id = await _active_conn_id()
    if conn_id is None:
        await _reply(update, "no active connection")
        return
    override = await db.get_override(conn_id=conn_id, chat_id=chat_id)
    if not override or not override.get("style_fingerprint"):
        await _reply(update, "no style fingerprint yet (need 30+ owner-typed messages in this chat)")
        return
    sf = override["style_fingerprint"]
    if len(sf) > 3500:
        sf = sf[:3500] + "...(truncated)"
    await _reply(update, f"style fingerprint for {chat_id}:\n{sf}")


async def on_purge(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Wipe memory rows for a chat (or all chats). Keeps messages + overrides.
    Extraction will rebuild from messages on the next run."""
    if not _is_owner(update):
        return
    args = ctx.args or []
    conn_id = await _active_conn_id()
    if conn_id is None:
        await _reply(update, "no active connection")
        return
    conn = db._db()
    if not args or args[0] == "all":
        cur = await conn.execute(
            "SELECT COUNT(*) AS n FROM contact_memory WHERE conn_id = ?",
            (conn_id,),
        )
        row = await cur.fetchone()
        n = int(row["n"]) if row else 0
        await conn.execute("DELETE FROM contact_memory WHERE conn_id = ?", (conn_id,))
        await conn.execute(
            "UPDATE messages SET extracted_at = NULL WHERE conn_id = ?",
            (conn_id,),
        )
        # Profile was derived from the now-gone memory — clear it so it rebuilds clean.
        await conn.execute(
            "UPDATE contact_overrides "
            "SET profile=NULL, profile_updated_at=NULL, profile_memory_count_at_update=NULL "
            "WHERE conn_id = ?",
            (conn_id,),
        )
        await conn.commit()
        await _reply(update, f"💣 purged {n} memory rows across all chats. extraction + profile will rebuild.")
        return
    chat_id = _parse_int(args[0])
    if chat_id is None:
        await _reply(update, "usage: /purge <chat_id> | all")
        return
    cur = await conn.execute(
        "SELECT COUNT(*) AS n FROM contact_memory WHERE conn_id = ? AND chat_id = ?",
        (conn_id, chat_id),
    )
    row = await cur.fetchone()
    n = int(row["n"]) if row else 0
    await conn.execute(
        "DELETE FROM contact_memory WHERE conn_id = ? AND chat_id = ?",
        (conn_id, chat_id),
    )
    await conn.execute(
        "UPDATE messages SET extracted_at = NULL WHERE conn_id = ? AND chat_id = ?",
        (conn_id, chat_id),
    )
    await conn.execute(
        "UPDATE contact_overrides "
        "SET profile=NULL, profile_updated_at=NULL, profile_memory_count_at_update=NULL "
        "WHERE conn_id = ? AND chat_id = ?",
        (conn_id, chat_id),
    )
    await conn.commit()
    await _reply(update, f"💣 purged {n} memory rows for chat {chat_id}. extraction + profile will rebuild.")


async def on_wipe(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Purge messages + memory + summary for one chat. Keeps overrides (tag, nickname)."""
    if not _is_owner(update):
        return
    args = ctx.args or []
    if not args:
        await _reply(update, "usage: /wipe <chat_id>  (purges messages + memory + summary for that chat)")
        return
    chat_id = _parse_int(args[0])
    if chat_id is None:
        await _reply(update, "bad chat_id")
        return
    conn_id = await _active_conn_id()
    if conn_id is None:
        await _reply(update, "no active connection")
        return
    conn = db._db()
    await conn.execute("DELETE FROM messages WHERE conn_id=? AND chat_id=?", (conn_id, chat_id))
    await conn.execute("DELETE FROM contact_memory WHERE conn_id=? AND chat_id=?", (conn_id, chat_id))
    await conn.execute("DELETE FROM chat_summaries WHERE conn_id=? AND chat_id=?", (conn_id, chat_id))
    # Clear derived profile too (it was built from the now-gone memory).
    # Keep relationship/nickname/style — those are owner-curated.
    await conn.execute(
        "UPDATE contact_overrides "
        "SET profile=NULL, profile_updated_at=NULL, profile_memory_count_at_update=NULL "
        "WHERE conn_id=? AND chat_id=?",
        (conn_id, chat_id),
    )
    await conn.commit()
    await _reply(update, f"🧹 purged messages/memory/summary/profile for chat {chat_id}. overrides kept.")


async def on_backup(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_owner(update):
        return
    src = Path(settings.db_path).resolve()
    if not src.exists():
        await _reply(update, "db file not found")
        return
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dst = src.with_name(f"{src.stem}.{stamp}.bak{src.suffix}")
    try:
        shutil.copy2(src, dst)
    except Exception as e:  # noqa: BLE001
        await _reply(update, f"backup failed: {e}")
        return
    size_kb = dst.stat().st_size // 1024
    await _reply(update, f"💾 db backed up → {dst.name} ({size_kb} KB)")


# ---------------------------------------------------------------------------
# registration
# ---------------------------------------------------------------------------


def register(app: Application) -> None:
    app.add_handler(CommandHandler("help", on_help))
    # state
    app.add_handler(CommandHandler("pause", on_pause))
    app.add_handler(CommandHandler("resume", on_resume))
    app.add_handler(CommandHandler("status", on_status))
    app.add_handler(CommandHandler("stats", on_stats))
    app.add_handler(CommandHandler("last", on_last))
    # contacts
    app.add_handler(CommandHandler("who", on_who))
    app.add_handler(CommandHandler("contacts", on_contacts))
    app.add_handler(CommandHandler("senders", on_senders))
    app.add_handler(CommandHandler("find", on_find))
    # memory / facts
    app.add_handler(CommandHandler("prompt", on_prompt))
    app.add_handler(CommandHandler("note", on_note))
    app.add_handler(CommandHandler("memory", on_memory))
    app.add_handler(CommandHandler("forget", on_forget))
    app.add_handler(CommandHandler("extract", on_extract))
    app.add_handler(CommandHandler("style", on_style))
    app.add_handler(CommandHandler("profile", on_profile))
    app.add_handler(CommandHandler("purge", on_purge))
    app.add_handler(CommandHandler("wipe", on_wipe))
    # persona files
    app.add_handler(CommandHandler("persona", on_persona))
    app.add_handler(CommandHandler("reload", on_reload))
    # modes
    app.add_handler(CommandHandler("approval", on_approval))
    app.add_handler(CommandHandler("innercircle", on_innercircle))
    app.add_handler(CommandHandler("voice", on_voice))
    app.add_handler(CommandHandler("quiet", on_quiet))
    # tuning
    app.add_handler(CommandHandler("delay", on_delay))
    app.add_handler(CommandHandler("away_delay", on_away_delay))
    app.add_handler(CommandHandler("cooldown", on_cooldown))
    # tools
    app.add_handler(CommandHandler("preview", on_preview))
    app.add_handler(CommandHandler("say", on_say))
    app.add_handler(CommandHandler("pending", on_pending))
    app.add_handler(CommandHandler("backup", on_backup))
    # HITL approval regex
    app.add_handler(MessageHandler(filters.Regex(r"^/approve_\d+$"), on_approve))
    app.add_handler(MessageHandler(filters.Regex(r"^/edit_\d+(\s|$)"), on_edit))
    app.add_handler(MessageHandler(filters.Regex(r"^/skip_\d+$"), on_skip))
