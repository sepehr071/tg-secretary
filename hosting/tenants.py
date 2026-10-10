"""Tenant lifecycle: files on disk, OpenRouter key, pm2 process."""
from __future__ import annotations

import asyncio
import json
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


NEVER_KEYS = ("promise", "meet", "money", "private")
_TONE_TXT = ("formal", "neutral", "casual and friendly")
_LEN_TXT = ("short messages", "longer, detailed messages")
_EMOJI_TXT = ("no emoji", "a few emoji", "lots of emoji")
_NEVER_TXT = {"promise": "make promises", "meet": "lock in plans to meet", "money": "talk about money",
              "private": "share my personal information"}


def _write_tone(tid: int, tone: int | None, length: int | None, emoji: int | None, never_keys: list[str]) -> None:
    """Merge default + never into prompts/tone.json (keeps per-group keys the dashboard saved). Atomic."""
    path = _ensure_dir(tid) / "prompts" / "tone.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError):
        data = {}
    if tone is not None and length is not None and emoji is not None:
        data["default"] = {"tone": tone, "len": length, "emoji": emoji}
    data["never"] = [k for k in NEVER_KEYS if k in never_keys]
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    os.replace(tmp, path)


async def save_profile(tid: int, first_name: str, about: str, style: str, never: str, *, tone: int | None = None,
                       length: int | None = None, emoji: int | None = None, never_keys: list[str] | None = None) -> None:
    t = await db.get_tenant(tid)
    if t is None:
        raise ValueError(f"no tenant {tid}")
    keys = never_keys or []
    how = ", ".join([txt[v] for v, txt in ((tone, _TONE_TXT), (length, _LEN_TXT), (emoji, _EMOJI_TXT)) if v is not None])
    parts = [about.strip()]
    if how or style.strip():
        parts.append("How I write: " + ". ".join(x for x in (how, style.strip()) if x))
    nevers = [_NEVER_TXT[k] for k in NEVER_KEYS if k in keys]
    if never.strip():
        nevers.append(never.strip())
    if nevers:
        parts.append("Never: " + "; ".join(nevers))
    (_ensure_dir(tid) / "prompts" / "about_me.txt").write_text("\n\n".join(p for p in parts if p) + "\n", encoding="utf-8")
    _write_tone(tid, tone, length, emoji, keys)
    write_env(tid, {
        "TG_BOT_TOKEN": settings.platform_bot_token, "OWNER_USER_ID": str(t["owner_tg_id"]),
        "OWNER_FIRST_NAME": first_name.strip() or "the owner",
        "HOSTED": "1", "DASHBOARD_ROOT_PATH": "/app", "DASHBOARD_LANG": "fa",
    })
    await db.update_tenant(tid, profile_done=1)


async def ensure_key(tid: int) -> str:
    """Give the tenant its model credentials once. OpenRouter: a zero-limit key per tenant
    (returns its hash). Claude: the shared platform key in the tenant .env (returns "")."""
    t = await db.get_tenant(tid)
    if t is None:
        raise ValueError(f"no tenant {tid}")
    if settings.provider == "anthropic":
        write_env(tid, {"ANTHROPIC_API_KEY": settings.anthropic_api_key, "ANTHROPIC_MODEL": settings.anthropic_model})
        return ""
    if t["or_key_hash"]:
        return t["or_key_hash"]
    key, key_hash = await openrouter.create_key(pm2.name(tid), 0.0)
    write_env(tid, {"OPENROUTER_API_KEY": key})  # shown once: persist before anything else
    await db.update_tenant(tid, or_key_hash=key_hash)
    return key_hash


async def sync_limit(tid: int) -> None:
    """Set the credit cap to the absolute sum of the tenant's payments; safe to repeat.
    Claude: the cap lives in the tenant .env, so a running tenant restarts to read it."""
    h = await ensure_key(tid)
    total = await db.payments_total(tid)
    if settings.provider == "anthropic":
        write_env(tid, {"CREDIT_LIMIT_USD": f"{total:.4f}"})
        t = await db.get_tenant(tid)
        if t and t["status"] == "running":
            await asyncio.to_thread(pm2.restart, tid)
    else:
        await openrouter.set_limit(h, total)
    await db.update_tenant(tid, warned_at_limit=None)


def _spend(path: Path) -> float:
    """Recorded Claude spend in a tenant DB (read-only); 0 before the bot created its tables."""
    if not path.exists():
        return 0.0
    con = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        row = con.execute("SELECT COALESCE(SUM(cost_usd), 0) FROM llm_usage").fetchone()
        return float(row[0]) if row else 0.0
    except sqlite3.OperationalError:
        return 0.0
    finally:
        con.close()


async def credit(t: dict) -> dict | None:
    """{limit, usage, limit_remaining} for a tenant row, or None when unknown.
    Same shape as OpenRouter's key info so the pages and the alert loop don't care."""
    if settings.provider == "anthropic":
        limit = await db.payments_total(t["id"])
        used = await asyncio.to_thread(_spend, tenant_dir(t["id"]) / "secretary.db")
        return {"limit": limit, "usage": used, "limit_remaining": limit - used}
    if not t["or_key_hash"]:
        return None
    try:
        return await openrouter.get_key(t["or_key_hash"])
    except openrouter.OpenRouterError as e:
        log.warning("credit lookup failed for tenant %s: %s", t["id"], e)
        return None


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
