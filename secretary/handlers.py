import asyncio
import logging
import time
from typing import Any

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import ContextTypes

from . import db, innercircle, llm
from . import memory as memory_module
from . import voice as voice_module
from .config import settings
from .prompts import load_system_prompt

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# connection lifecycle
# ---------------------------------------------------------------------------


async def on_business_connection(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Fires when the owner connects/disconnects the bot in Telegram Business settings."""
    bc = update.business_connection
    if bc is None:
        return

    rights = bc.rights.to_dict() if getattr(bc, "rights", None) else None
    can_reply = bool(getattr(bc, "can_reply", False) or (rights and rights.get("can_reply", False)))

    await db.upsert_connection(
        conn_id=bc.id,
        owner_user_id=bc.user.id,
        owner_chat_id=bc.user_chat_id,
        can_reply=can_reply,
        is_enabled=bool(bc.is_enabled),
        rights=rights,
    )

    log.info(
        "business_connection: id=%s owner=%s enabled=%s can_reply=%s",
        bc.id, bc.user.id, bc.is_enabled, can_reply,
    )


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


async def _notify_owner(
    ctx: ContextTypes.DEFAULT_TYPE, owner_chat_id: int, text: str
) -> None:
    """Send a DM to the owner directly (not via business connection)."""
    try:
        await ctx.bot.send_message(chat_id=owner_chat_id, text=text)
    except Exception:  # noqa: BLE001
        log.exception("owner notify failed")


async def _capture_profile(conn_id: str, chat_id: int, msg: Any) -> None:
    """Snapshot the sender's Telegram profile (first/last name + username) into
    contact_overrides on every inbound message. Cheap upsert; ignores empties."""
    u = getattr(msg, "from_user", None)
    if u is None:
        return
    try:
        await db.upsert_contact_profile(
            conn_id=conn_id,
            chat_id=chat_id,
            first_name=getattr(u, "first_name", None),
            last_name=getattr(u, "last_name", None),
            username=getattr(u, "username", None),
        )
    except Exception:
        log.exception("capture_profile failed (chat=%s)", chat_id)


def _contact_name(msg: Any) -> str:
    u = getattr(msg, "from_user", None)
    if u is None:
        return "unknown"
    name = (u.full_name or "").strip() if hasattr(u, "full_name") else ""
    if name:
        return name
    if getattr(u, "username", None):
        return f"@{u.username}"
    return str(u.id)


def _attachment_kind(msg: Any) -> str:
    if getattr(msg, "photo", None):
        return "photo"
    if getattr(msg, "sticker", None):
        return "sticker"
    if getattr(msg, "video", None):
        return "video"
    if getattr(msg, "video_note", None):
        return "video_note"
    if getattr(msg, "animation", None):
        return "animation"
    if getattr(msg, "document", None):
        return "document"
    if getattr(msg, "audio", None):
        return "audio"
    if getattr(msg, "voice", None):
        return "voice"
    if getattr(msg, "poll", None):
        return "poll"
    if getattr(msg, "contact", None):
        return "contact"
    if getattr(msg, "location", None):
        return "location"
    return "attachment"


def _in_quiet_window(now_ts: int, start_hhmm: str, end_hhmm: str) -> bool:
    try:
        sh, sm = (int(x) for x in start_hhmm.split(":"))
        eh, em = (int(x) for x in end_hhmm.split(":"))
    except (ValueError, AttributeError):
        return False
    lt = time.localtime(now_ts)
    cur = lt.tm_hour * 60 + lt.tm_min
    start = sh * 60 + sm
    end = eh * 60 + em
    if start == end:
        return False
    if start < end:
        return start <= cur < end
    # wraps midnight
    return cur >= start or cur < end


async def _last_inbound_ts(conn_id: str, chat_id: int) -> int | None:
    """Latest created_at among prior role='user' messages (excluding the just-inserted one)."""
    conn = db._db()
    cur = await conn.execute(
        """
        SELECT created_at FROM messages
        WHERE conn_id = ? AND chat_id = ? AND role = 'user'
        ORDER BY id DESC
        LIMIT 1 OFFSET 1
        """,
        (conn_id, chat_id),
    )
    row = await cur.fetchone()
    return row["created_at"] if row else None


async def _live_int(key: str, fallback: int) -> int:
    """Read a non-negative integer override from bot_state; fall back to env-default."""
    raw = await db.get_state(key)
    if not raw:
        return fallback
    try:
        n = int(raw)
        return n if n >= 0 else fallback
    except ValueError:
        return fallback


async def _voice_enabled() -> bool:
    """Live override for VOICE_TRANSCRIBE — owner can toggle via /voice on|off."""
    override = await db.get_state("voice_override")
    if override == "on":
        return True
    if override == "off":
        return False
    return settings.voice_transcribe


async def _is_away_mode(*, conn_id: str, chat_id: int) -> bool:
    """True if owner appears away in this chat: last bot reply newer than last
    owner-typed message. Returns False if owner has never been seen here (full
    delay applies on first contact) or if owner typed more recently than the bot.
    """
    conn = db._db()
    cur = await conn.execute(
        """
        SELECT via_bot, created_at FROM messages
        WHERE conn_id = ? AND chat_id = ? AND role = 'assistant'
        ORDER BY id DESC
        LIMIT 1
        """,
        (conn_id, chat_id),
    )
    row = await cur.fetchone()
    if row is None:
        return False
    return bool(row["via_bot"])


async def _find_message_row(
    conn_id: str, chat_id: int, tg_message_id: int
) -> dict[str, Any] | None:
    conn = db._db()
    cur = await conn.execute(
        """
        SELECT * FROM messages
        WHERE conn_id = ? AND chat_id = ? AND tg_message_id = ?
        LIMIT 1
        """,
        (conn_id, chat_id, tg_message_id),
    )
    row = await cur.fetchone()
    return dict(row) if row else None


async def _update_message_edited(message_id: int, content: str, edited_at: int) -> None:
    conn = db._db()
    await conn.execute(
        "UPDATE messages SET content = ?, edited_at = ? WHERE id = ?",
        (content, edited_at, message_id),
    )
    await conn.commit()


async def _mark_message_deleted(message_id: int, deleted_at: int) -> None:
    conn = db._db()
    await conn.execute(
        "UPDATE messages SET deleted_at = ? WHERE id = ?",
        (deleted_at, message_id),
    )
    await conn.commit()


def _format_approval_prompt(
    *, contact_name: str, chat_id: int, contact_msg: str, draft: str, pending_id: int
) -> str:
    return (
        f"\U0001f4e9 From {contact_name} (chat {chat_id}):\n"
        f"\"{contact_msg}\"\n\n"
        f"✏️ Draft:\n"
        f"\"{draft}\"\n\n"
        f"/approve_{pending_id}  /edit_{pending_id} <text>  /skip_{pending_id}"
    )


async def _try_read_business_message(
    ctx: ContextTypes.DEFAULT_TYPE, *, conn_id: str, chat_id: int, message_id: int
) -> None:
    """Mark the incoming message as read; tolerate older PTB by falling back to raw post."""
    try:
        fn = getattr(ctx.bot, "read_business_message", None)
        if fn is not None:
            await fn(business_connection_id=conn_id, chat_id=chat_id, message_id=message_id)
            return
        post = getattr(ctx.bot, "_post", None)
        if post is not None:
            await post(
                "readBusinessMessage",
                {
                    "business_connection_id": conn_id,
                    "chat_id": chat_id,
                    "message_id": message_id,
                },
            )
    except Exception as e:  # noqa: BLE001
        log.debug("read_business_message failed: %s", e)


# ---------------------------------------------------------------------------
# text pipeline (shared by text + transcribed voice)
# ---------------------------------------------------------------------------


async def _handle_inbound_text(
    ctx: ContextTypes.DEFAULT_TYPE,
    msg: Any,
    conn_id: str,
    chat_id: int,
    text_content: str,
) -> None:
    """Shared inbound text pipeline. Caller has already inserted the user-role row."""
    arrived_at = int(time.time())

    conn_row = await db.get_connection(conn_id)
    if not conn_row:
        return
    owner_chat_id = conn_row["owner_chat_id"]

    # Global pause
    if await db.get_state_bool("paused"):
        return

    override = await db.get_override(conn_id=conn_id, chat_id=chat_id)

    # Per-chat pause
    if override and override.get("paused"):
        return

    # Quiet hours
    quiet_start = await db.get_state("quiet_start")
    quiet_end = await db.get_state("quiet_end")
    if quiet_start and quiet_end and _in_quiet_window(arrived_at, quiet_start, quiet_end):
        log.info("quiet hours active — chat %s skipped", chat_id)
        return

    # Owner-active cooldown (live-tunable via /cooldown)
    cooldown_seconds = await _live_int(
        "cooldown_override", settings.owner_active_cooldown_seconds
    )
    cooldown_since = arrived_at - cooldown_seconds
    if await db.owner_active_since(
        conn_id=conn_id, chat_id=chat_id, since_ts=cooldown_since
    ):
        log.info(
            "chat %s within owner-active cooldown (%ds) — skipping",
            chat_id, cooldown_seconds,
        )
        return

    # Race guard delay: full delay when owner is "active" (last manual msg newer
    # than last bot reply); short "away" delay once the bot has already replied
    # without owner preemption. Resets to full delay the moment owner types again.
    # Both delays are live-tunable via /delay and /away_delay.
    away_mode = await _is_away_mode(conn_id=conn_id, chat_id=chat_id)
    if away_mode:
        delay = await _live_int(
            "away_delay_override", settings.away_reply_delay_seconds
        )
    else:
        delay = await _live_int(
            "delay_override", settings.auto_reply_delay_seconds
        )
    if delay > 0:
        log.debug(
            "delaying auto-reply for chat %s by %ds (away_mode=%s)",
            chat_id, delay, away_mode,
        )
        await asyncio.sleep(delay)
        if await db.owner_active_since(
            conn_id=conn_id, chat_id=chat_id, since_ts=arrived_at
        ):
            log.info("owner replied manually during delay — aborting bot reply")
            return

    relationship = (override.get("relationship") if override else None) or "unknown"
    persona_extra = override.get("persona_extra") if override else None
    style_fingerprint = override.get("style_fingerprint") if override else None
    contact_name = (override.get("nickname") if override else None) or _contact_name(msg)

    # Inner-circle escalation gate
    gate_enabled = await db.get_state_bool("innercircle_gate", default=True)
    last_inbound = await _last_inbound_ts(conn_id, chat_id)
    escalate, reason = innercircle.should_escalate(
        text=text_content,
        relationship=relationship,
        last_inbound_ts=last_inbound,
        gate_enabled=gate_enabled,
        now_ts=arrived_at,
    )

    approval_mode = await db.get_state_bool("approval_mode")
    needs_approval = escalate or approval_mode

    history = await db.load_history(
        conn_id=conn_id, chat_id=chat_id, limit=settings.history_turns * 2
    )
    if history and history[-1]["role"] == "user" and history[-1]["content"] == text_content:
        history = history[:-1]

    memory_block = await memory_module.render_memory_block(conn_id, chat_id)
    system_prompt = load_system_prompt(
        chat_id=chat_id,
        relationship=relationship,
        persona_extra=persona_extra,
        memory_block=memory_block,
        style_fingerprint=style_fingerprint,
    )

    if not needs_approval:
        try:
            await ctx.bot.send_chat_action(
                chat_id=chat_id,
                action=ChatAction.TYPING,
                business_connection_id=conn_id,
            )
        except Exception as e:  # noqa: BLE001
            log.debug("typing action failed: %s", e)

    summary_row = await db.get_summary(conn_id=conn_id, chat_id=chat_id)
    summary_text = summary_row["summary"] if summary_row else None

    try:
        reply_text = await llm.generate_reply(
            system_prompt=system_prompt,
            history=history,
            user_message=text_content,
            summary=summary_text,
        )
    except Exception:
        log.exception("LLM call failed")
        return

    # Empty-reply path
    if reply_text is None or reply_text.strip() == "":
        await _notify_owner(
            ctx,
            owner_chat_id,
            f"\U0001f4e9 From {contact_name} (chat {chat_id}): LLM returned empty — no reply sent.",
        )
        await db.enqueue(conn_id=conn_id, chat_id=chat_id)
        return

    if needs_approval:
        pending_id = await db.create_pending(
            conn_id=conn_id,
            chat_id=chat_id,
            contact_name=contact_name,
            contact_msg=text_content,
            draft=reply_text,
        )
        prefix = ""
        if escalate and reason:
            prefix = f"⚠️ Escalated: {reason}\n\n"
        await _notify_owner(
            ctx,
            owner_chat_id,
            prefix + _format_approval_prompt(
                contact_name=contact_name,
                chat_id=chat_id,
                contact_msg=text_content,
                draft=reply_text,
                pending_id=pending_id,
            ),
        )
        await db.enqueue(conn_id=conn_id, chat_id=chat_id)
        return

    # Final race guard before sending
    if await db.owner_active_since(
        conn_id=conn_id, chat_id=chat_id, since_ts=arrived_at
    ):
        log.info("owner replied manually during LLM call — aborting send")
        return

    sent = await ctx.bot.send_message(
        chat_id=chat_id,
        text=reply_text,
        business_connection_id=conn_id,
    )

    await db.append_message(
        conn_id=conn_id,
        chat_id=chat_id,
        role="assistant",
        content=reply_text,
        tg_message_id=sent.message_id,
        via_bot=True,
    )

    await _try_read_business_message(
        ctx, conn_id=conn_id, chat_id=chat_id, message_id=msg.message_id
    )
    await db.enqueue(conn_id=conn_id, chat_id=chat_id)

    log.info("replied in chat %s (%d chars)", chat_id, len(reply_text))


# ---------------------------------------------------------------------------
# business message handlers
# ---------------------------------------------------------------------------


async def on_business_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Fires when someone DMs the owner's account on a connected chat."""
    msg = update.business_message
    if msg is None or not msg.text:
        return

    conn_id = msg.business_connection_id
    if not conn_id:
        return

    sender_id = msg.from_user.id if msg.from_user else None
    chat_id = msg.chat.id

    # Owner-skip guard: don't auto-reply to messages the owner typed manually.
    if sender_id == settings.owner_user_id:
        await db.append_message(
            conn_id=conn_id,
            chat_id=chat_id,
            role="assistant",
            content=msg.text,
            tg_message_id=msg.message_id,
            via_bot=False,
        )
        log.debug("owner-authored message in chat %s — cooldown reset", chat_id)
        return

    conn_row = await db.get_connection(conn_id)
    if not conn_row or not conn_row["is_enabled"] or not conn_row["can_reply"]:
        log.info("connection %s not allowed to reply — skipping", conn_id)
        return

    await _capture_profile(conn_id, chat_id, msg)

    await db.append_message(
        conn_id=conn_id,
        chat_id=chat_id,
        role="user",
        content=msg.text,
        tg_message_id=msg.message_id,
    )

    await _handle_inbound_text(ctx, msg, conn_id, chat_id, msg.text)


async def on_business_voice(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle voice attachments on business messages."""
    msg = update.business_message
    if msg is None or not getattr(msg, "voice", None):
        return

    conn_id = msg.business_connection_id
    if not conn_id:
        return

    sender_id = msg.from_user.id if msg.from_user else None
    chat_id = msg.chat.id
    voice = msg.voice
    log.info(
        "voice handler fired: chat=%s sender=%s duration=%ss file_id=%s",
        chat_id, sender_id, getattr(voice, "duration", "?"),
        getattr(voice, "file_id", "?"),
    )

    # Owner-skip: log a placeholder marker so cooldown counts it.
    if sender_id == settings.owner_user_id:
        log.info("voice: owner-authored, storing placeholder + cooldown reset")
        await db.append_message(
            conn_id=conn_id,
            chat_id=chat_id,
            role="assistant",
            content="[voice]",
            tg_message_id=msg.message_id,
            via_bot=False,
        )
        return

    conn_row = await db.get_connection(conn_id)
    if not conn_row or not conn_row["is_enabled"] or not conn_row["can_reply"]:
        log.info("voice: connection not allowed to reply, skipping")
        return
    owner_chat_id = conn_row["owner_chat_id"]

    await _capture_profile(conn_id, chat_id, msg)

    # Cooldown short-circuit
    arrived_at = int(time.time())
    cooldown_seconds = await _live_int(
        "cooldown_override", settings.owner_active_cooldown_seconds
    )
    cooldown_since = arrived_at - cooldown_seconds
    if await db.owner_active_since(
        conn_id=conn_id, chat_id=chat_id, since_ts=cooldown_since
    ):
        log.info("voice: within owner-active cooldown (%ds), skipping", cooldown_seconds)
        await db.append_message(
            conn_id=conn_id, chat_id=chat_id, role="user",
            content="[voice]", tg_message_id=msg.message_id,
        )
        return

    contact_name = _contact_name(msg)

    voice_on = await _voice_enabled()
    if not voice_on:
        log.info("voice: VOICE_TRANSCRIBE disabled (env/override), notifying owner")
    elif voice.duration > 120:
        log.info("voice: duration %ss exceeds 120s cap, notifying owner", voice.duration)
    if not voice_on or voice.duration > 120:
        await db.append_message(
            conn_id=conn_id, chat_id=chat_id, role="user",
            content="[voice]", tg_message_id=msg.message_id,
        )
        await _notify_owner(
            ctx, owner_chat_id,
            f"\U0001f4e9 From {contact_name} (chat {chat_id}) sent a voice "
            f"({voice.duration}s) — transcription skipped, bot stayed silent.",
        )
        return

    log.info("voice: downloading file_id=%s", voice.file_id)

    try:
        file = await ctx.bot.get_file(voice.file_id)
        ogg_bytes = bytes(await file.download_as_bytearray())
    except Exception:
        log.exception("voice download failed")
        await db.append_message(
            conn_id=conn_id, chat_id=chat_id, role="user",
            content="[voice]", tg_message_id=msg.message_id,
        )
        await _notify_owner(
            ctx, owner_chat_id,
            f"\U0001f4e9 From {contact_name} (chat {chat_id}) sent a voice — "
            f"download failed, bot stayed silent.",
        )
        return

    transcript = await voice_module.transcribe(ogg_bytes)
    if transcript is None or not transcript.strip():
        await db.append_message(
            conn_id=conn_id, chat_id=chat_id, role="user",
            content="[voice]", tg_message_id=msg.message_id,
        )
        await _notify_owner(
            ctx, owner_chat_id,
            f"\U0001f4e9 From {contact_name} (chat {chat_id}) sent a voice — "
            f"transcription failed, bot stayed silent.",
        )
        return

    text_content = f"[voice transcript] {transcript}"
    await db.append_message(
        conn_id=conn_id, chat_id=chat_id, role="user",
        content=text_content, tg_message_id=msg.message_id,
    )

    await _handle_inbound_text(ctx, msg, conn_id, chat_id, text_content)


async def on_business_non_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Photo/sticker/video/document/poll/animation: store placeholder + notify owner. Never reply."""
    msg = update.business_message
    if msg is None:
        return

    conn_id = msg.business_connection_id
    if not conn_id:
        return

    sender_id = msg.from_user.id if msg.from_user else None
    chat_id = msg.chat.id
    kind = _attachment_kind(msg)

    if sender_id == settings.owner_user_id:
        await db.append_message(
            conn_id=conn_id, chat_id=chat_id, role="assistant",
            content=f"[{kind}]", tg_message_id=msg.message_id, via_bot=False,
        )
        return

    conn_row = await db.get_connection(conn_id)
    if not conn_row:
        return
    owner_chat_id = conn_row["owner_chat_id"]

    await _capture_profile(conn_id, chat_id, msg)

    await db.append_message(
        conn_id=conn_id, chat_id=chat_id, role="user",
        content=f"[{kind}]", tg_message_id=msg.message_id,
    )

    override = await db.get_override(conn_id=conn_id, chat_id=chat_id)
    contact_name = (override.get("nickname") if override else None) or _contact_name(msg)

    await _notify_owner(
        ctx, owner_chat_id,
        f"\U0001f4e9 {contact_name} sent a {kind} — bot stayed silent.",
    )


async def on_business_edited(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner-side edit of a previously stored business message."""
    msg = update.edited_business_message
    if msg is None:
        return

    conn_id = msg.business_connection_id
    chat_id = msg.chat.id if msg.chat else None
    if not conn_id or chat_id is None or msg.message_id is None:
        return

    row = await _find_message_row(conn_id, chat_id, msg.message_id)
    if row is None:
        return

    now = int(time.time())
    new_content = msg.text if msg.text is not None else (msg.caption or row["content"])
    await _update_message_edited(row["id"], new_content, now)
    await db.add_memory(
        conn_id=conn_id,
        chat_id=chat_id,
        kind="event",
        content=f"contact edited message at {now}",
        source_msg_id=row["id"],
    )


async def on_business_deleted(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner-side delete batch for a business connection."""
    payload = update.deleted_business_messages
    if payload is None:
        return

    conn_id = getattr(payload, "business_connection_id", None)
    chat = getattr(payload, "chat", None)
    chat_id = chat.id if chat is not None else None
    ids = getattr(payload, "message_ids", None) or []
    if not conn_id or chat_id is None or not ids:
        return

    now = int(time.time())
    for tg_msg_id in ids:
        row = await _find_message_row(conn_id, chat_id, tg_msg_id)
        if row is None:
            continue
        await _mark_message_deleted(row["id"], now)
        if (now - row["created_at"]) > 60:
            truncated = (row["content"] or "")[:120]
            await db.add_memory(
                conn_id=conn_id,
                chat_id=chat_id,
                kind="event",
                content=f"contact deleted message: '{truncated}'",
                source_msg_id=row["id"],
            )


# ---------------------------------------------------------------------------
# /start
# ---------------------------------------------------------------------------


async def on_start(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Plain /start command in a regular (non-business) DM to the bot itself."""
    if update.effective_message is None:
        return
    await update.effective_message.reply_text(
        "Hi. I'm a secretary bot. To use me, open Telegram → Settings → "
        "Business → Chatbots, and enter my username."
    )
