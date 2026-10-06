"""Tenant lifecycle: files on disk, OpenRouter key, pm2 process."""
from __future__ import annotations

import asyncio
import os
import secrets
import shutil
import sqlite3
from pathlib import Path

from dotenv import set_key

from . import db, openrouter, pm2, tg
from .config import settings


def tenant_dir(tid: int) -> Path:
    return settings.tenants_root / str(tid)


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


async def attach_bot(tenant: dict, token: str, managed: bool) -> dict:
    me = await tg.call(token, "getMe")
    await db.set_tenant_bot(tenant["id"], me["id"], me.get("username", ""), managed)
    user = await db.get_user(tenant["owner_tg_id"])
    write_env(tenant["id"], {
        "TG_BOT_TOKEN": token, "OWNER_USER_ID": str(tenant["owner_tg_id"]),
        "OWNER_FIRST_NAME": (user or {}).get("first_name") or "the owner",
        "HOSTED": "1", "DASHBOARD_ROOT_PATH": "/app", "DASHBOARD_LANG": "fa",
    })
    if tenant["status"] == "running":
        await asyncio.to_thread(pm2.restart, tenant["id"])
    return me


async def save_profile(tid: int, first_name: str, about: str, style: str, never: str) -> None:
    parts = [about.strip()]
    if style.strip():
        parts.append(f"How I write: {style.strip()}")
    if never.strip():
        parts.append(f"Never: {never.strip()}")
    (_ensure_dir(tid) / "prompts" / "about_me.txt").write_text("\n\n".join(p for p in parts if p) + "\n", encoding="utf-8")
    write_env(tid, {"OWNER_FIRST_NAME": first_name.strip() or "the owner"})
    await db.update_tenant(tid, profile_done=1)


async def activate(tid: int, credit_usd: float) -> None:
    """Idempotent: a retry after a partial failure reuses the key, port and secret."""
    t = await db.get_tenant(tid)
    if t is None:
        raise ValueError(f"no tenant {tid}")
    if not t["or_key_hash"]:
        key, key_hash = await openrouter.create_key(pm2.name(tid), credit_usd)
        write_env(tid, {"OPENROUTER_API_KEY": key})  # shown once: persist before anything else
        await db.update_tenant(tid, or_key_hash=key_hash)
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
    if not await db.add_payment(tid, amount_usd, paid_text, note, admin_tg_id, client_ref):
        return False
    t = await db.get_tenant(tid)
    if t and t["or_key_hash"]:
        info = await openrouter.get_key(t["or_key_hash"])
        await openrouter.set_limit(t["or_key_hash"], float(info.get("limit") or 0) + amount_usd)
        await db.update_tenant(tid, warned_at_limit=None)
    if t and t["status"] == "awaiting_credit":
        await activate(tid, amount_usd)
    return True


async def delete(tid: int) -> None:
    t = await db.get_tenant(tid)
    if t is None:
        return
    await asyncio.to_thread(pm2.delete, tid)
    if t["or_key_hash"]:
        await openrouter.disable_key(t["or_key_hash"])
    if t["managed"] and t["bot_id"]:
        # Kill the old token so nothing we stored can still drive the bot.
        await tg.call(settings.platform_bot_token, "replaceManagedBotToken", user_id=t["bot_id"])
    shutil.rmtree(tenant_dir(tid), ignore_errors=True)
    await db.release_tenant(tid)


def _connected(path: Path, owner: int) -> bool:
    if not path.exists():
        return False
    con = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        return con.execute("SELECT 1 FROM connections WHERE owner_user_id=? AND is_enabled=1",
                           (owner,)).fetchone() is not None
    except sqlite3.OperationalError:  # bot hasn't created its tables yet
        return False
    finally:
        con.close()


async def is_connected(tid: int) -> bool:
    t = await db.get_tenant(tid)
    return bool(t) and await asyncio.to_thread(_connected, tenant_dir(tid) / "secretary.db", t["owner_tg_id"])
