"""Replay engine for the local web tester.

Given an uploaded DB + chat + (start_msg_id, end_msg_id) slice + N (model,
reasoning) columns, iterate every contact (`role=user`) message in the slice
and regenerate the bot reply for each column in parallel. Strict replay — the
history each column sees is the EXACT prior history from the DB, never any
generated reply. Apples-to-apples model comparison.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from secretary import db, llm, memory, prompts
from secretary.config import settings

log = logging.getLogger(__name__)


@dataclass
class Column:
    idx: int
    model: str
    reasoning_effort: str | None = None


@dataclass
class TurnStart:
    user_msg_id: int
    user_msg: str
    user_msg_ts: int
    original_bot_reply: str | None
    original_bot_msg_id: int | None
    columns: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class ColumnDone:
    user_msg_id: int
    column_idx: int
    new_reply: str
    latency_ms: int
    error: str | None = None


async def _slice_messages(
    *, conn_id: str, chat_id: int, start_id: int, end_id: int
) -> list[dict[str, Any]]:
    cur = await db._db().execute(
        """
        SELECT id, role, content, via_bot, created_at FROM messages
        WHERE conn_id = ? AND chat_id = ? AND id BETWEEN ? AND ?
        ORDER BY id ASC
        """,
        (conn_id, chat_id, start_id, end_id),
    )
    rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def _next_bot_reply_after(
    *, conn_id: str, chat_id: int, after_id: int
) -> dict[str, Any] | None:
    cur = await db._db().execute(
        """
        SELECT id, content FROM messages
        WHERE conn_id = ? AND chat_id = ?
          AND role = 'assistant' AND via_bot = 1
          AND id > ?
        ORDER BY id ASC
        LIMIT 1
        """,
        (conn_id, chat_id, after_id),
    )
    row = await cur.fetchone()
    return dict(row) if row else None


async def _resolve_conn_id(chat_id: int) -> str | None:
    cur = await db._db().execute(
        "SELECT conn_id FROM messages WHERE chat_id = ? ORDER BY id ASC LIMIT 1",
        (chat_id,),
    )
    row = await cur.fetchone()
    return row["conn_id"] if row else None


async def _call_one_column(
    *,
    column: Column,
    system_prompt: str,
    history: list[dict[str, Any]],
    user_message: str,
    user_msg_id: int,
) -> ColumnDone:
    t0 = time.monotonic()
    new_reply = ""
    error: str | None = None
    try:
        new_reply = await llm.generate_reply(
            system_prompt=system_prompt,
            history=history,
            user_message=user_message,
            model=column.model,
            reasoning_effort=column.reasoning_effort,
        )
    except Exception as e:  # noqa: BLE001 — surfaces the OpenRouter error verbatim
        error = f"{type(e).__name__}: {e}"
        log.warning(
            "replay column failed col=%d model=%s msg_id=%s: %s",
            column.idx, column.model, user_msg_id, error,
        )
    latency_ms = int((time.monotonic() - t0) * 1000)
    return ColumnDone(
        user_msg_id=user_msg_id,
        column_idx=column.idx,
        new_reply=new_reply,
        latency_ms=latency_ms,
        error=error,
    )


async def run_replay(
    *,
    chat_id: int,
    start_msg_id: int,
    end_msg_id: int,
    columns: list[Column],
) -> AsyncIterator[TurnStart | ColumnDone]:
    """Yield TurnStart + N ColumnDone events for every role=user message in
    [start_msg_id..end_msg_id]. Columns within a turn run in parallel; turns
    are sequential so the UI can stream in chronological order."""
    if not columns:
        return
    conn_id = await _resolve_conn_id(chat_id)
    if conn_id is None:
        return

    override = await db.get_override(conn_id=conn_id, chat_id=chat_id)
    relationship = (override or {}).get("relationship") or "unknown"
    persona_extra = (override or {}).get("persona_extra")
    style_fingerprint = (override or {}).get("style_fingerprint")

    memory_block = await memory.render_memory_block(conn_id, chat_id)
    system_prompt = prompts.load_system_prompt(
        chat_id=chat_id,
        relationship=relationship,
        persona_extra=persona_extra,
        memory_block=memory_block,
        style_fingerprint=style_fingerprint,
    )

    msgs = await _slice_messages(
        conn_id=conn_id, chat_id=chat_id, start_id=start_msg_id, end_id=end_msg_id,
    )

    for row in msgs:
        if row["role"] != "user":
            continue
        history = await db.load_history_before(
            conn_id=conn_id,
            chat_id=chat_id,
            before_id=row["id"],
            limit=settings.history_turns,
        )
        original = await _next_bot_reply_after(
            conn_id=conn_id, chat_id=chat_id, after_id=row["id"],
        )

        yield TurnStart(
            user_msg_id=row["id"],
            user_msg=row["content"],
            user_msg_ts=row["created_at"],
            original_bot_reply=(original or {}).get("content"),
            original_bot_msg_id=(original or {}).get("id"),
            columns=[
                {"idx": c.idx, "model": c.model, "reasoning_effort": c.reasoning_effort}
                for c in columns
            ],
        )

        # Fan out the N columns in parallel; emit each as it completes so the UI
        # paints fast columns first and the slow column doesn't block siblings.
        tasks = [
            asyncio.create_task(
                _call_one_column(
                    column=c,
                    system_prompt=system_prompt,
                    history=history,
                    user_message=row["content"],
                    user_msg_id=row["id"],
                )
            )
            for c in columns
        ]
        for fut in asyncio.as_completed(tasks):
            yield await fut


def event_to_jsonable(ev: TurnStart | ColumnDone) -> tuple[str, dict[str, Any]]:
    """Return (sse_event_name, payload_dict) for one engine event."""
    if isinstance(ev, TurnStart):
        return "turn_start", {
            "user_msg_id": ev.user_msg_id,
            "user_msg": ev.user_msg,
            "user_msg_ts": ev.user_msg_ts,
            "original_bot_reply": ev.original_bot_reply,
            "original_bot_msg_id": ev.original_bot_msg_id,
            "columns": ev.columns,
        }
    return "column_done", {
        "user_msg_id": ev.user_msg_id,
        "column_idx": ev.column_idx,
        "new_reply": ev.new_reply,
        "latency_ms": ev.latency_ms,
        "error": ev.error,
    }
