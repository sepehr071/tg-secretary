"""Tenant lifecycle: files on disk, OpenRouter key, pm2 process."""
from __future__ import annotations

import asyncio
import logging
import os
import secrets
import shutil
import sqlite3
from pathlib import Path

from dotenv import set_key

from . import db, openrouter, pm2
from .config import settings

log = logging.getLogger(__name__)
_locks: dict[int, asyncio.Lock] = {}


def tenant_dir(tid: int) -> Path:
    return (settings.tenants_root / str(tid)).resolve()


def _ensure_dir(tid: int) -> Path:
    d = tenant_dir(tid)
    (d / "prompts" / "personas").mkdir(parents=True, exist_ok=True)
    (d / "prompts" / "contacts").mkdir(exist_ok=True)
    (d / "logs").mkdir(exist_ok=True)
    # Only the shipped examples. The repo owner's real <rel>.txt personas must never leak in.
    for f in (settings.repo_root / "prompts" / "personas").glob("*.example.txt"):
        shutil.copyfile(f, d / "prompts" / "personas" / f.name)
    return d


def write_env(tid: int, values: dict[str, str]) -> None:
    env = _ensure_dir(tid) / ".env"
    env.touch(exist_ok=True)
    if os.name == "posix":
        env.chmod(0o600)
    for k, v in values.items():
        set_key(env, k, v)  # quotes values, so names with spaces or '#' are safe


async def save_profile(tid: int, first_name: str, about: str, style: str, never: str) -> None:
    t = await db.get_tenant(tid)
    if t is None:
        raise ValueError(f"no tenant {tid}")
    parts = [about.strip()]
    if style.strip():
        parts.append(f"How I write: {style.strip()}")
    if never.strip():
        parts.append(f"Never: {never.strip()}")
    (_ensure_dir(tid) / "prompts" / "about_me.txt").write_text("\n\n".join(p for p in parts if p) + "\n", encoding="utf-8")
    write_env(tid, {
        "TG_BOT_TOKEN": settings.platform_bot_token, "OWNER_USER_ID": str(t["owner_tg_id"]),
        "OWNER_FIRST_NAME": first_name.strip() or "the owner",
        "HOSTED": "1", "DASHBOARD_ROOT_PATH": "/app", "DASHBOARD_LANG": "fa",
    })
    await db.update_tenant(tid, profile_done=1)


async def ensure_key(tid: int) -> str:
    """Create the tenant's zero-limit OpenRouter key once; returns its hash."""
    t = await db.get_tenant(tid)
    if t is None:
        raise ValueError(f"no tenant {tid}")
    if t["or_key_hash"]:
        return t["or_key_hash"]
    key, key_hash = await openrouter.create_key(pm2.name(tid), 0.0)
    write_env(tid, {"OPENROUTER_API_KEY": key})  # shown once: persist before anything else
    await db.update_tenant(tid, or_key_hash=key_hash)
    return key_hash


async def sync_limit(tid: int) -> None:
    """Set the key limit to the absolute sum of the tenant's payments; safe to repeat."""
    h = await ensure_key(tid)
    await openrouter.set_limit(h, await db.payments_total(tid))
    await db.update_tenant(tid, warned_at_limit=None)


async def activate(tid: int) -> None:
    """Idempotent: a retry after a partial failure reuses the key, port and secret."""
    await ensure_key(tid)
    t = await db.get_tenant(tid)
    port = await db.alloc_port(tid)
    secret = t["proxy_secret"] or secrets.token_urlsafe(32)
    write_env(tid, {"DASHBOARD_PORT": str(port), "DASHBOARD_PROXY_SECRET": secret})
    await db.update_tenant(tid, proxy_secret=secret)
    try:
        await asyncio.to_thread(pm2.start, tid, tenant_dir(tid))
    except pm2.Pm2Error as e:
        await db.update_tenant(tid, last_error=f"pm2 start: {e}")
        raise
    await db.update_tenant(tid, status="running", last_error=None)


async def top_up(tid: int, amount_usd: float, paid_text: str, note: str,
                 admin_tg_id: int, client_ref: str) -> bool:
    """True = new payment row. Safe to retry with the same client_ref after any failure:
    credit is applied once (payments.applied) and activation is re-attempted."""
    t = await db.get_tenant(tid)
    if t is None or not t["profile_done"]:
        raise ValueError("tenant not ready")
    async with _locks.setdefault(tid, asyncio.Lock()):
        inserted = await db.add_payment(tid, amount_usd, paid_text, note, admin_tg_id, client_ref)
        p = await db.get_payment(client_ref)
        if p and not p["applied"]:
            await sync_limit(tid)
            await db.mark_payment_applied(client_ref)
        t = await db.get_tenant(tid)
        if t and t["status"] == "awaiting_credit" and t["profile_done"]:
            await activate(tid)
    return inserted


async def delete(tid: int) -> None:
    """Best-effort external cleanup: a failing step is recorded, never blocks removing the secrets."""
    t = await db.get_tenant(tid)
    if t is None:
        return
    errors = []
    try:
        await asyncio.to_thread(pm2.delete, tid)
    except Exception as e:  # noqa: BLE001
        errors.append(f"pm2 delete: {type(e).__name__}")
    if t["or_key_hash"]:
        try:
            await openrouter.disable_key(t["or_key_hash"])
        except Exception as e:  # noqa: BLE001
            errors.append(f"disable key: {type(e).__name__}")
    if errors:  # type names only: exception text can carry URLs
        log.warning("tenant %s delete incomplete: %s", tid, "; ".join(errors))
        await db.update_tenant(tid, last_error="delete: " + "; ".join(errors))
    try:
        shutil.rmtree(tenant_dir(tid))
    except FileNotFoundError:
        pass
    except OSError as e:
        log.warning("could not remove %s (holds secrets): %s", tenant_dir(tid), e)
    await db.release_tenant(tid)


def _connection_state(path: Path, owner: int) -> str:
    if not path.exists():
        return "none"
    con = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        row = con.execute("SELECT MAX(can_reply) FROM connections WHERE owner_user_id=? AND is_enabled=1",
                          (owner,)).fetchone()
    except sqlite3.OperationalError:  # bot hasn't created its tables yet
        return "none"
    finally:
        con.close()
    if row is None or row[0] is None:
        return "none"
    return "ok" if row[0] else "no_reply"


async def connection_state(tid: int) -> str:
    """"none" | "no_reply" (connected without the reply right) | "ok"."""
    t = await db.get_tenant(tid)
    if not t:
        return "none"
    return await asyncio.to_thread(_connection_state, tenant_dir(tid) / "secretary.db", t["owner_tg_id"])
