import json
import time
from typing import Any

import aiosqlite

from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS connections (
    conn_id        TEXT PRIMARY KEY,
    owner_user_id  INTEGER NOT NULL,
    owner_chat_id  INTEGER NOT NULL,
    can_reply      INTEGER NOT NULL,
    is_enabled     INTEGER NOT NULL,
    rights_json    TEXT,
    updated_at     INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    conn_id        TEXT NOT NULL,
    chat_id        INTEGER NOT NULL,
    role           TEXT NOT NULL,
    content        TEXT NOT NULL,
    tg_message_id  INTEGER,
    via_bot        INTEGER NOT NULL DEFAULT 0,
    created_at     INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_messages_chat
    ON messages(conn_id, chat_id, created_at);

CREATE TABLE IF NOT EXISTS chat_summaries (
    conn_id TEXT NOT NULL,
    chat_id INTEGER NOT NULL,
    summary TEXT NOT NULL,
    summarized_up_to_msg_id INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (conn_id, chat_id)
);

CREATE TABLE IF NOT EXISTS contact_overrides (
    conn_id TEXT NOT NULL,
    chat_id INTEGER NOT NULL,
    paused INTEGER NOT NULL DEFAULT 0,
    persona_extra TEXT,
    nickname TEXT,
    relationship TEXT NOT NULL DEFAULT 'unknown',
    style_fingerprint TEXT,
    style_updated_at INTEGER,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (conn_id, chat_id)
);

CREATE TABLE IF NOT EXISTS contact_memory (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conn_id TEXT NOT NULL,
    chat_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    content TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 1.0,
    source_msg_id INTEGER,
    created_at INTEGER NOT NULL,
    expires_at INTEGER,
    superseded_by INTEGER
);
CREATE INDEX IF NOT EXISTS idx_memory_chat_kind ON contact_memory(conn_id, chat_id, kind);

CREATE TABLE IF NOT EXISTS extraction_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conn_id TEXT NOT NULL,
    chat_id INTEGER NOT NULL,
    requested_at INTEGER NOT NULL,
    processed_at INTEGER,
    status TEXT NOT NULL DEFAULT 'pending'
);
CREATE INDEX IF NOT EXISTS idx_extraction_pending ON extraction_queue(status, requested_at);

CREATE TABLE IF NOT EXISTS pending_replies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conn_id TEXT NOT NULL,
    chat_id INTEGER NOT NULL,
    contact_name TEXT,
    contact_msg TEXT NOT NULL,
    draft TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pending_status ON pending_replies(status, expires_at);

CREATE TABLE IF NOT EXISTS bot_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at INTEGER NOT NULL
);
"""

# Idempotent column adds for users upgrading an existing DB.
MIGRATE_VIA_BOT = "ALTER TABLE messages ADD COLUMN via_bot INTEGER NOT NULL DEFAULT 0;"
MIGRATE_DELETED_AT = "ALTER TABLE messages ADD COLUMN deleted_at INTEGER;"
MIGRATE_EDITED_AT = "ALTER TABLE messages ADD COLUMN edited_at INTEGER;"
MIGRATE_EXTRACTED_AT = "ALTER TABLE messages ADD COLUMN extracted_at INTEGER;"
MIGRATE_TG_FIRST_NAME = "ALTER TABLE contact_overrides ADD COLUMN tg_first_name TEXT;"
MIGRATE_TG_LAST_NAME = "ALTER TABLE contact_overrides ADD COLUMN tg_last_name TEXT;"
MIGRATE_TG_USERNAME = "ALTER TABLE contact_overrides ADD COLUMN tg_username TEXT;"


_conn: aiosqlite.Connection | None = None


async def init_db() -> aiosqlite.Connection:
    """Open shared connection, set pragmas, run migrations. Idempotent."""
    global _conn
    if _conn is None:
        _conn = await aiosqlite.connect(settings.db_path)
        _conn.row_factory = aiosqlite.Row
        await _conn.execute("PRAGMA journal_mode=WAL")
        await _conn.execute("PRAGMA synchronous=NORMAL")
    await _conn.executescript(SCHEMA)
    for stmt in (
        MIGRATE_VIA_BOT, MIGRATE_DELETED_AT, MIGRATE_EDITED_AT, MIGRATE_EXTRACTED_AT,
        MIGRATE_TG_FIRST_NAME, MIGRATE_TG_LAST_NAME, MIGRATE_TG_USERNAME,
    ):
        try:
            await _conn.execute(stmt)
        except aiosqlite.OperationalError:
            pass  # column already exists
    await _conn.commit()
    return _conn


def _db() -> aiosqlite.Connection:
    if _conn is None:
        raise RuntimeError("db not initialized; call init_db() first")
    return _conn


async def close_db() -> None:
    global _conn
    if _conn is not None:
        await _conn.close()
        _conn = None


# ---------------------------------------------------------------------------
# connections
# ---------------------------------------------------------------------------


async def upsert_connection(
    *,
    conn_id: str,
    owner_user_id: int,
    owner_chat_id: int,
    can_reply: bool,
    is_enabled: bool,
    rights: dict[str, Any] | None,
) -> None:
    db = _db()
    await db.execute(
        """
        INSERT INTO connections
            (conn_id, owner_user_id, owner_chat_id, can_reply, is_enabled, rights_json, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(conn_id) DO UPDATE SET
            owner_user_id=excluded.owner_user_id,
            owner_chat_id=excluded.owner_chat_id,
            can_reply=excluded.can_reply,
            is_enabled=excluded.is_enabled,
            rights_json=excluded.rights_json,
            updated_at=excluded.updated_at
        """,
        (
            conn_id,
            owner_user_id,
            owner_chat_id,
            int(can_reply),
            int(is_enabled),
            json.dumps(rights) if rights else None,
            int(time.time()),
        ),
    )
    await db.commit()


async def get_connection(conn_id: str) -> dict[str, Any] | None:
    db = _db()
    cur = await db.execute("SELECT * FROM connections WHERE conn_id = ?", (conn_id,))
    row = await cur.fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# messages
# ---------------------------------------------------------------------------


async def append_message(
    *,
    conn_id: str,
    chat_id: int,
    role: str,
    content: str,
    tg_message_id: int | None = None,
    via_bot: bool = False,
) -> None:
    db = _db()
    await db.execute(
        """
        INSERT INTO messages
            (conn_id, chat_id, role, content, tg_message_id, via_bot, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            conn_id, chat_id, role, content, tg_message_id,
            int(via_bot), int(time.time()),
        ),
    )
    await db.commit()


async def owner_active_since(
    *, conn_id: str, chat_id: int, since_ts: int
) -> bool:
    """True if the owner manually sent any message in this chat after `since_ts`."""
    db = _db()
    cur = await db.execute(
        """
        SELECT 1 FROM messages
        WHERE conn_id = ? AND chat_id = ?
          AND role = 'assistant' AND via_bot = 0
          AND created_at > ?
        LIMIT 1
        """,
        (conn_id, chat_id, since_ts),
    )
    return (await cur.fetchone()) is not None


async def load_history(
    *, conn_id: str, chat_id: int, limit: int
) -> list[dict[str, str]]:
    """Return last `limit` messages (oldest-first) for this chat, in OpenAI message format."""
    db = _db()
    cur = await db.execute(
        """
        SELECT role, content FROM messages
        WHERE conn_id = ? AND chat_id = ?
        ORDER BY id DESC
        LIMIT ?
        """,
        (conn_id, chat_id, limit),
    )
    rows = list(await cur.fetchall())
    rows.reverse()
    return [{"role": r["role"], "content": r["content"]} for r in rows]


async def load_history_with_summary(
    *, conn_id: str, chat_id: int, limit: int
) -> tuple[str | None, list[dict[str, str]]]:
    """Return (summary, messages). Summary is the persisted chat_summaries row text, or None."""
    summary_row = await get_summary(conn_id=conn_id, chat_id=chat_id)
    summary = summary_row["summary"] if summary_row else None
    messages = await load_history(conn_id=conn_id, chat_id=chat_id, limit=limit)
    return summary, messages


# ---------------------------------------------------------------------------
# chat_summaries
# ---------------------------------------------------------------------------


async def get_summary(*, conn_id: str, chat_id: int) -> dict[str, Any] | None:
    db = _db()
    cur = await db.execute(
        "SELECT * FROM chat_summaries WHERE conn_id = ? AND chat_id = ?",
        (conn_id, chat_id),
    )
    row = await cur.fetchone()
    return dict(row) if row else None


async def upsert_summary(
    *,
    conn_id: str,
    chat_id: int,
    summary: str,
    summarized_up_to_msg_id: int,
) -> None:
    db = _db()
    await db.execute(
        """
        INSERT INTO chat_summaries
            (conn_id, chat_id, summary, summarized_up_to_msg_id, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(conn_id, chat_id) DO UPDATE SET
            summary=excluded.summary,
            summarized_up_to_msg_id=excluded.summarized_up_to_msg_id,
            updated_at=excluded.updated_at
        """,
        (conn_id, chat_id, summary, summarized_up_to_msg_id, int(time.time())),
    )
    await db.commit()


# ---------------------------------------------------------------------------
# contact_overrides
# ---------------------------------------------------------------------------


_OVERRIDE_COLUMNS = {
    "paused",
    "persona_extra",
    "nickname",
    "relationship",
    "style_fingerprint",
    "style_updated_at",
    "tg_first_name",
    "tg_last_name",
    "tg_username",
}


async def upsert_contact_profile(
    *,
    conn_id: str,
    chat_id: int,
    first_name: str | None,
    last_name: str | None,
    username: str | None,
) -> None:
    """Cheap upsert of Telegram-provided profile fields from inbound messages.
    Only overwrites when value is non-empty (so a temp missing username doesn't wipe it).
    """
    fields: dict[str, Any] = {}
    if first_name:
        fields["tg_first_name"] = first_name
    if last_name:
        fields["tg_last_name"] = last_name
    if username:
        fields["tg_username"] = username
    if not fields:
        return
    await upsert_override(conn_id=conn_id, chat_id=chat_id, **fields)


async def get_override(*, conn_id: str, chat_id: int) -> dict[str, Any] | None:
    db = _db()
    cur = await db.execute(
        "SELECT * FROM contact_overrides WHERE conn_id = ? AND chat_id = ?",
        (conn_id, chat_id),
    )
    row = await cur.fetchone()
    return dict(row) if row else None


async def upsert_override(*, conn_id: str, chat_id: int, **fields: Any) -> None:
    """Insert-or-update the override row, writing only the keys passed in `fields`."""
    unknown = set(fields) - _OVERRIDE_COLUMNS
    if unknown:
        raise ValueError(f"unknown override columns: {unknown}")
    db = _db()
    now = int(time.time())
    # Build insert with defaults + supplied fields.
    insert_cols = ["conn_id", "chat_id", "updated_at"]
    insert_vals: list[Any] = [conn_id, chat_id, now]
    for key, val in fields.items():
        if key == "paused":
            val = int(bool(val))
        insert_cols.append(key)
        insert_vals.append(val)
    placeholders = ", ".join(["?"] * len(insert_cols))
    set_clause = ", ".join(
        f"{k}=excluded.{k}" for k in insert_cols if k not in ("conn_id", "chat_id")
    )
    await db.execute(
        f"""
        INSERT INTO contact_overrides ({", ".join(insert_cols)})
        VALUES ({placeholders})
        ON CONFLICT(conn_id, chat_id) DO UPDATE SET {set_clause}
        """,
        insert_vals,
    )
    await db.commit()


async def set_paused(*, conn_id: str, chat_id: int, paused: bool) -> None:
    await upsert_override(conn_id=conn_id, chat_id=chat_id, paused=int(paused))


async def set_relationship(
    *, conn_id: str, chat_id: int, relationship: str, nickname: str | None = None
) -> None:
    fields: dict[str, Any] = {"relationship": relationship}
    if nickname is not None:
        fields["nickname"] = nickname
    await upsert_override(conn_id=conn_id, chat_id=chat_id, **fields)


async def set_persona_extra(*, conn_id: str, chat_id: int, text: str) -> None:
    await upsert_override(conn_id=conn_id, chat_id=chat_id, persona_extra=text)


async def set_style_fingerprint(*, conn_id: str, chat_id: int, json_str: str) -> None:
    await upsert_override(
        conn_id=conn_id,
        chat_id=chat_id,
        style_fingerprint=json_str,
        style_updated_at=int(time.time()),
    )


# ---------------------------------------------------------------------------
# contact_memory
# ---------------------------------------------------------------------------


async def add_memory(
    *,
    conn_id: str,
    chat_id: int,
    kind: str,
    content: str,
    source_msg_id: int | None = None,
    expires_at: int | None = None,
    confidence: float = 1.0,
) -> int:
    db = _db()
    cur = await db.execute(
        """
        INSERT INTO contact_memory
            (conn_id, chat_id, kind, content, confidence, source_msg_id, created_at, expires_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            conn_id, chat_id, kind, content, confidence,
            source_msg_id, int(time.time()), expires_at,
        ),
    )
    await db.commit()
    if cur.lastrowid is None:
        raise RuntimeError("contact_memory insert returned no lastrowid")
    return cur.lastrowid


async def list_memory(*, conn_id: str, chat_id: int) -> list[dict[str, Any]]:
    """Return non-expired, non-superseded memory rows for this chat."""
    db = _db()
    now = int(time.time())
    cur = await db.execute(
        """
        SELECT * FROM contact_memory
        WHERE conn_id = ? AND chat_id = ?
          AND superseded_by IS NULL
          AND (expires_at IS NULL OR expires_at > ?)
        ORDER BY created_at DESC
        """,
        (conn_id, chat_id, now),
    )
    rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def expire_memory(memory_id: int) -> None:
    db = _db()
    await db.execute(
        "UPDATE contact_memory SET expires_at = ? WHERE id = ?",
        (int(time.time()), memory_id),
    )
    await db.commit()


async def supersede(old_id: int, new_id: int) -> None:
    db = _db()
    await db.execute(
        "UPDATE contact_memory SET superseded_by = ? WHERE id = ?",
        (new_id, old_id),
    )
    await db.commit()


# ---------------------------------------------------------------------------
# extraction_queue
# ---------------------------------------------------------------------------


async def enqueue(*, conn_id: str, chat_id: int) -> None:
    """Insert a pending extraction request; no-op if one exists in last 10 min."""
    db = _db()
    now = int(time.time())
    cur = await db.execute(
        """
        SELECT 1 FROM extraction_queue
        WHERE conn_id = ? AND chat_id = ? AND status = 'pending'
          AND requested_at > ?
        LIMIT 1
        """,
        (conn_id, chat_id, now - 600),
    )
    if await cur.fetchone() is not None:
        return
    await db.execute(
        """
        INSERT INTO extraction_queue (conn_id, chat_id, requested_at, status)
        VALUES (?, ?, ?, 'pending')
        """,
        (conn_id, chat_id, now),
    )
    await db.commit()


async def claim_next() -> dict[str, Any] | None:
    """Atomically mark the oldest pending row as processing, return it."""
    db = _db()
    cur = await db.execute(
        """
        SELECT * FROM extraction_queue
        WHERE status = 'pending'
        ORDER BY requested_at ASC
        LIMIT 1
        """,
    )
    row = await cur.fetchone()
    if row is None:
        return None
    await db.execute(
        "UPDATE extraction_queue SET status = 'processing' WHERE id = ?",
        (row["id"],),
    )
    await db.commit()
    return dict(row)


async def mark_processed(queue_id: int) -> None:
    db = _db()
    await db.execute(
        "UPDATE extraction_queue SET status = 'done', processed_at = ? WHERE id = ?",
        (int(time.time()), queue_id),
    )
    await db.commit()


async def mark_failed(queue_id: int) -> None:
    db = _db()
    await db.execute(
        "UPDATE extraction_queue SET status = 'failed', processed_at = ? WHERE id = ?",
        (int(time.time()), queue_id),
    )
    await db.commit()


# ---------------------------------------------------------------------------
# pending_replies
# ---------------------------------------------------------------------------


async def create_pending(
    *,
    conn_id: str,
    chat_id: int,
    contact_name: str | None,
    contact_msg: str,
    draft: str,
    ttl_seconds: int = 3600,
) -> int:
    db = _db()
    now = int(time.time())
    cur = await db.execute(
        """
        INSERT INTO pending_replies
            (conn_id, chat_id, contact_name, contact_msg, draft, status, created_at, expires_at)
        VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)
        """,
        (conn_id, chat_id, contact_name, contact_msg, draft, now, now + ttl_seconds),
    )
    await db.commit()
    if cur.lastrowid is None:
        raise RuntimeError("pending_replies insert returned no lastrowid")
    return cur.lastrowid


async def get_pending(pending_id: int) -> dict[str, Any] | None:
    db = _db()
    cur = await db.execute(
        "SELECT * FROM pending_replies WHERE id = ?", (pending_id,)
    )
    row = await cur.fetchone()
    return dict(row) if row else None


async def set_pending_status(pending_id: int, status: str) -> None:
    db = _db()
    await db.execute(
        "UPDATE pending_replies SET status = ? WHERE id = ?",
        (status, pending_id),
    )
    await db.commit()


async def list_pending() -> list[dict[str, Any]]:
    db = _db()
    cur = await db.execute(
        "SELECT * FROM pending_replies WHERE status = 'pending' ORDER BY created_at ASC"
    )
    rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def expire_old() -> None:
    db = _db()
    await db.execute(
        """
        UPDATE pending_replies
        SET status = 'expired'
        WHERE status = 'pending' AND expires_at <= ?
        """,
        (int(time.time()),),
    )
    await db.commit()


# ---------------------------------------------------------------------------
# bot_state
# ---------------------------------------------------------------------------


_TRUE_STRINGS = {"on", "true", "1", "yes"}


async def get_state(key: str, default: str | None = None) -> str | None:
    db = _db()
    cur = await db.execute("SELECT value FROM bot_state WHERE key = ?", (key,))
    row = await cur.fetchone()
    return row["value"] if row else default


async def set_state(key: str, value: str) -> None:
    db = _db()
    await db.execute(
        """
        INSERT INTO bot_state (key, value, updated_at) VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET
            value=excluded.value,
            updated_at=excluded.updated_at
        """,
        (key, value, int(time.time())),
    )
    await db.commit()


async def get_state_bool(key: str, default: bool = False) -> bool:
    raw = await get_state(key)
    if raw is None:
        return default
    return raw.strip().lower() in _TRUE_STRINGS


async def set_state_bool(key: str, value: bool) -> None:
    await set_state(key, "on" if value else "off")
