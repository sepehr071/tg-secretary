"""Hosting control-plane storage: users, sessions, tenants, payments. One shared connection."""
from __future__ import annotations

import hashlib
import secrets
import sqlite3
import time
from pathlib import Path
from typing import Any

import aiosqlite

from .config import settings

SESSION_TTL = 30 * 86400
OAUTH_TTL = 600

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    tg_id INTEGER PRIMARY KEY, first_name TEXT, username TEXT, created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS consents (
    id INTEGER PRIMARY KEY, tg_id INTEGER NOT NULL, version INTEGER NOT NULL, accepted_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY, tg_id INTEGER NOT NULL, expires_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS oauth_states (
    state TEXT PRIMARY KEY, verifier TEXT NOT NULL, expires_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS tenants (
    id INTEGER PRIMARY KEY,
    owner_tg_id INTEGER NOT NULL,
    bot_id INTEGER UNIQUE,
    bot_username TEXT,
    managed INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'draft',
    profile_done INTEGER NOT NULL DEFAULT 0,
    dashboard_port INTEGER UNIQUE,
    proxy_secret TEXT,
    or_key_hash TEXT,
    warned_at_limit REAL,
    last_error TEXT,
    created_at INTEGER NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS one_live_tenant ON tenants(owner_tg_id) WHERE status!='deleted';
CREATE TABLE IF NOT EXISTS payments (
    id INTEGER PRIMARY KEY, tenant_id INTEGER NOT NULL, amount_usd REAL NOT NULL,
    paid_text TEXT, note TEXT, admin_tg_id INTEGER NOT NULL,
    client_ref TEXT NOT NULL UNIQUE, created_at INTEGER NOT NULL,
    applied INTEGER NOT NULL DEFAULT 0
);
"""

UPDATABLE = {"bot_username", "status", "profile_done", "proxy_secret", "or_key_hash",
             "warned_at_limit", "last_error", "managed"}

_conn: aiosqlite.Connection | None = None


class BotTaken(Exception):
    """The bot is already attached to another tenant."""


def _db() -> aiosqlite.Connection:
    if _conn is None:
        raise RuntimeError("hosting.db.init() not called")
    return _conn


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def init(path: Path) -> None:
    global _conn
    _conn = await aiosqlite.connect(path)
    _conn.row_factory = aiosqlite.Row
    await _conn.execute("PRAGMA journal_mode=WAL")
    await _conn.executescript(SCHEMA)
    await _conn.commit()


async def close() -> None:
    global _conn
    if _conn is not None:
        await _conn.close()
        _conn = None


async def _one(sql: str, *args: Any) -> dict[str, Any] | None:
    async with _db().execute(sql, args) as cur:
        row = await cur.fetchone()
    return dict(row) if row else None


async def _write(sql: str, *args: Any) -> int | None:
    cur = await _db().execute(sql, args)
    await _db().commit()
    return cur.lastrowid


async def upsert_user(tg_id: int, first_name: str, username: str | None) -> None:
    await _write(
        "INSERT INTO users (tg_id, first_name, username, created_at) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(tg_id) DO UPDATE SET first_name=excluded.first_name, username=excluded.username",
        tg_id, first_name, username, int(time.time()),
    )


async def get_user(tg_id: int) -> dict[str, Any] | None:
    return await _one("SELECT * FROM users WHERE tg_id=?", tg_id)


async def add_consent(tg_id: int, version: int) -> None:
    await _write("INSERT INTO consents (tg_id, version, accepted_at) VALUES (?, ?, ?)",
                 tg_id, version, int(time.time()))


async def has_consent(tg_id: int, version: int) -> bool:
    return await _one("SELECT 1 FROM consents WHERE tg_id=? AND version=?", tg_id, version) is not None


async def create_session(tg_id: int) -> str:
    token = secrets.token_urlsafe(32)
    await _write("INSERT INTO sessions VALUES (?, ?, ?)", _hash(token), tg_id, int(time.time()) + SESSION_TTL)
    return token


async def session_user(token: str | None) -> int | None:
    if not token:
        return None
    row = await _one("SELECT tg_id FROM sessions WHERE token_hash=? AND expires_at>?", _hash(token), int(time.time()))
    return row["tg_id"] if row else None


async def end_session(token: str | None) -> None:
    if token:
        await _write("DELETE FROM sessions WHERE token_hash=?", _hash(token))


async def put_oauth_state(state: str, verifier: str) -> None:
    now = int(time.time())
    await _write("DELETE FROM oauth_states WHERE expires_at<?", now)
    await _write("INSERT INTO oauth_states VALUES (?, ?, ?)", state, verifier, now + OAUTH_TTL)


async def pop_oauth_state(state: str) -> str | None:
    now = int(time.time())
    if sqlite3.sqlite_version_info >= (3, 35):  # atomic: two callbacks can't both consume a state
        cur = await _db().execute(
            "DELETE FROM oauth_states WHERE state=? AND expires_at>? RETURNING verifier", (state, now))
        row = await cur.fetchone()
        await _db().commit()
        return row["verifier"] if row else None
    row = await _one("SELECT verifier FROM oauth_states WHERE state=? AND expires_at>?", state, now)
    await _write("DELETE FROM oauth_states WHERE state=?", state)
    return row["verifier"] if row else None


async def create_tenant(owner_tg_id: int) -> int:
    tid = await _write("INSERT INTO tenants (owner_tg_id, created_at) VALUES (?, ?)", owner_tg_id, int(time.time()))
    if tid is None:
        raise RuntimeError("tenant insert returned no rowid")
    return tid


async def get_tenant(tid: int) -> dict[str, Any] | None:
    return await _one("SELECT * FROM tenants WHERE id=?", tid)


async def get_tenant_by_owner(tg_id: int) -> dict[str, Any] | None:
    return await _one("SELECT * FROM tenants WHERE owner_tg_id=? AND status!='deleted'", tg_id)


async def get_tenant_by_bot(bot_id: int) -> dict[str, Any] | None:
    return await _one("SELECT * FROM tenants WHERE bot_id=?", bot_id)


async def list_tenants() -> list[dict[str, Any]]:
    async with _db().execute("SELECT * FROM tenants WHERE status!='deleted' ORDER BY id") as cur:
        return [dict(r) for r in await cur.fetchall()]


async def update_tenant(tid: int, **fields: Any) -> None:
    bad = fields.keys() - UPDATABLE
    if bad:
        raise ValueError(f"not updatable: {bad}")
    if not fields:
        return
    sets = ", ".join(f"{k}=?" for k in fields)
    await _write(f"UPDATE tenants SET {sets} WHERE id=?", *fields.values(), tid)


async def set_tenant_bot(tid: int, bot_id: int, bot_username: str, managed: bool) -> None:
    owner = await get_tenant_by_bot(bot_id)
    if owner is not None and owner["id"] != tid:
        raise BotTaken(bot_username)
    try:
        await _write("UPDATE tenants SET bot_id=?, bot_username=?, managed=? WHERE id=?",
                     bot_id, bot_username, int(managed), tid)
    except sqlite3.IntegrityError as e:  # lost a race with another attach
        raise BotTaken(bot_username) from e


async def alloc_port(tid: int) -> int:
    for _ in range(5):  # UNIQUE(dashboard_port) loses a race -> rescan
        t = await get_tenant(tid)
        if t and t["dashboard_port"]:
            return t["dashboard_port"]
        async with _db().execute("SELECT dashboard_port FROM tenants WHERE dashboard_port IS NOT NULL") as cur:
            used = {r[0] for r in await cur.fetchall()}
        port = next((p for p in range(settings.port_range_start, settings.port_range_end + 1)
                     if p not in used), None)
        if port is None:
            raise RuntimeError("no free dashboard port")
        try:
            await _write("UPDATE tenants SET dashboard_port=? WHERE id=?", port, tid)
            return port
        except sqlite3.IntegrityError:
            continue
    raise RuntimeError("could not allocate a dashboard port")


async def release_tenant(tid: int) -> None:
    await _write("UPDATE tenants SET status='deleted', bot_id=NULL, dashboard_port=NULL WHERE id=?", tid)


async def add_payment(tenant_id: int, amount_usd: float, paid_text: str, note: str,
                      admin_tg_id: int, client_ref: str) -> bool:
    """False when client_ref was already recorded (double-submitted form)."""
    try:
        await _write(
            "INSERT INTO payments (tenant_id, amount_usd, paid_text, note, admin_tg_id, client_ref, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            tenant_id, amount_usd, paid_text, note, admin_tg_id, client_ref, int(time.time()),
        )
    except sqlite3.IntegrityError:
        return False
    return True


async def get_payment(client_ref: str) -> dict[str, Any] | None:
    return await _one("SELECT * FROM payments WHERE client_ref=?", client_ref)


async def payments_total(tenant_id: int) -> float:
    row = await _one("SELECT COALESCE(SUM(amount_usd), 0) AS s FROM payments WHERE tenant_id=?", tenant_id)
    return float(row["s"]) if row else 0.0


async def mark_payment_applied(client_ref: str) -> None:
    await _write("UPDATE payments SET applied=1 WHERE client_ref=?", client_ref)
