"""Status and Settings pages."""
from __future__ import annotations

import asyncio
import json
import re
import time

import httpx
from fastapi import APIRouter, Request

from .. import db, setup
from ..config import settings
from .contacts import display_name
from .web import back, fa_digits, render, t

router = APIRouter()

# ponytail: flat guess (one short reply on the default model); upgrade = average the real cost from usage data.
AVG_REPLY_COST_USD = 0.008
DELAY_CHOICES = (10, 30, 120, 300)
_DELAY_LABELS = {10: "10 seconds", 30: "Half a minute", 120: "2 minutes", 300: "5 minutes"}
_ORB = {"ok": "ok", "paused": "paused", "no_credit": "danger"}  # anything else: warn

_RIGHTS = (("can_reply", "reply"), ("can_read_messages", "read messages"))


def _duration(seconds: float) -> str:
    minutes = int(seconds // 60)
    days, minutes = divmod(minutes, 1440)
    hours, minutes = divmod(minutes, 60)
    parts = ((days, "d"), (hours, "h")) if days else ((hours, "h"), (minutes, "m"))
    if settings.dashboard_lang == "fa":
        return fa_digits(" و ".join(f"{n} {t(unit)}" for n, unit in parts))
    return " ".join(f"{n}{unit}" for n, unit in parts)


def _health(connections: list[dict], paused: bool, credit: dict | None) -> str:
    """One word for the Status hero: what (if anything) stops replies right now."""
    if not connections:
        return "not_connected"
    c = connections[0]  # newest; the bot only uses the owner's latest connection
    if not c.get("is_enabled"):
        return "disabled"
    if "reply" in c["missing"]:
        return "no_reply"
    if credit and credit.get("limit_remaining") is not None and credit["limit_remaining"] <= 0.01:
        return "no_credit"
    return "paused" if paused else "ok"


def delay_label(seconds: int) -> str:
    return t(_DELAY_LABELS[seconds]) if seconds in _DELAY_LABELS else f"{fa_digits(seconds)} {t('seconds')}"


def _credit_view(credit: dict | None) -> dict | None:
    if credit is None:
        return None
    left, used = credit.get("limit_remaining"), credit.get("usage") or 0
    if left is None:
        return {"unlimited": True, "used": f"{used:.2f}"}
    left = max(left, 0)
    total = credit.get("limit") or (left + used)
    return {"unlimited": False, "left": f"{left:.2f}", "total": f"{total:.2f}",
            "pct": max(0, min(100, round(left / total * 100))) if total > 0 else 0,
            "replies": int(left / AVG_REPLY_COST_USD)}


async def _credit() -> dict | None:
    """Same shape as OpenRouter's /key: limit, usage, limit_remaining (None = no cap)."""
    if settings.provider == "anthropic":
        used = await db.usage_total()
        limit = settings.credit_limit_usd or None
        return {"limit": limit, "usage": used, "limit_remaining": (limit - used) if limit else None}
    try:
        return await asyncio.to_thread(setup.or_key_info, settings.openrouter_api_key, 5)
    except Exception:  # noqa: BLE001 — credit is informational; never break the page
        return None


@router.get("/")
async def status(request: Request):
    credit = await _credit()
    connections = await db.list_connections()
    for c in connections:
        rights = json.loads(c.get("rights_json") or "{}")
        c["missing"] = [label for key, label in _RIGHTS if not rights.get(key)]
    paused = await db.get_state_bool("paused")
    chats = {c["chat_id"]: c for c in await db.list_chats()}
    replies = await db.recent_reply_pairs(5)
    for r in replies:
        chat = chats.get(r["chat_id"], {})
        r["name"] = display_name(chat) if chat else str(r["chat_id"])
        r["rel"] = chat.get("relationship") or ""
    health = _health(connections, paused, credit)
    delay = int(await db.get_state("delay_override") or settings.auto_reply_delay_seconds)
    return render(
        request, "status.html",
        bot_username=request.app.state.bot.username,
        uptime=_duration(time.time() - request.app.state.started_at),
        model=settings.reply_model,
        paused=paused,
        approval=await db.get_state_bool("approval_mode"),
        innercircle=await db.get_state_bool("innercircle_gate", default=True),
        credit=_credit_view(credit),
        connections=connections,
        health=health,
        orb_state=_ORB.get(health, "warn"),
        delay_phrase=delay_label(delay),
        untagged=sum(1 for c in chats.values() if (c.get("relationship") or "unknown") == "unknown"),
        stats=await db.get_stats(),
        replies=replies,
        first_name=settings.owner_first_name,
    )


LIVE_INT_KEYS = {
    "delay_override": ("Reply delay (s)", "auto_reply_delay_seconds"),
    "away_delay_override": ("Away-mode delay (s)", "away_reply_delay_seconds"),
    "cooldown_override": ("Owner-active cooldown (s)", "owner_active_cooldown_seconds"),
    "retention_days": ("Days to keep raw messages", "message_retention_days"),
}
RETENTION_CHOICES = (1, 7, 30)
_RETENTION_LABELS = {0: "Forever", 1: "1 day", 7: "A week", 30: "A month"}
# Rendered as their own controls, not in the technical number list.
_OWN_CONTROL = {"delay_override", "retention_days"}
# .env keys the bot reads per call, so assigning them on `settings` applies at once.
LIVE_ENV_KEYS = ("OPENROUTER_MODEL", "EXTRACTOR_MODEL", "WHISPER_MODEL", "OWNER_FIRST_NAME", "HISTORY_TURNS")
RESTART_KEYS = {"TG_BOT_TOKEN", "OPENROUTER_API_KEY", "OWNER_USER_ID"}
# Whisper is an audio model and may be missing from /models, so only these are checked.
CHECKED_MODEL_KEYS = ("OPENROUTER_MODEL", "EXTRACTOR_MODEL")
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_models_cache: tuple[float, set[str] | None] = (0.0, None)


async def _model_ids() -> set[str] | None:
    if settings.provider == "anthropic":
        return None  # fixed model, no OpenRouter catalogue
    """OpenRouter model ids, cached 10 minutes (the list is large; None = unavailable)."""
    global _models_cache
    fetched_at, ids = _models_cache
    if time.time() - fetched_at > 600:
        ids = await asyncio.to_thread(setup.or_model_ids)
        _models_cache = (time.time(), ids)
    return ids


def _pad_time(t: str) -> str:
    """/quiet stores "1:00"; <input type=time> shows that as empty unless it's "01:00"."""
    h, _, m = t.partition(":")
    return f"{int(h):02d}:{m}" if h.isdigit() and m else t


def _to_int(raw: str) -> int | None:
    """Any Unicode digits (Persian keyboards type ۱۲۳) -> int; None if not a number."""
    try:
        return int(raw)
    except ValueError:
        return None


def _onoff(flag: bool) -> str:
    return "on" if flag else "off"


@router.get("/settings")
async def settings_page(request: Request):
    voice_raw = await db.get_state("voice_override") or ""
    quiet_start = _pad_time(await db.get_state("quiet_start") or "")
    quiet_end = _pad_time(await db.get_state("quiet_end") or "")
    delay = int(await db.get_state("delay_override") or settings.auto_reply_delay_seconds)
    choices = sorted({*DELAY_CHOICES, delay})  # a custom value (from /delay) stays visible
    retention = int(await db.get_state("retention_days") or settings.message_retention_days)
    retention_choices = sorted({*RETENTION_CHOICES, retention})
    return render(
        request, "settings.html",
        enabled=not await db.get_state_bool("paused"),
        approval=await db.get_state_bool("approval_mode"),
        innercircle=await db.get_state_bool("innercircle_gate", default=True),
        voice_on=(voice_raw or _onoff(settings.voice_transcribe)) == "on",
        voice_available=settings.provider != "anthropic",
        provider=settings.provider,
        quiet_on=bool(quiet_start and quiet_end),
        quiet_start=quiet_start or "23:00",
        quiet_end=quiet_end or "07:00",
        delay=delay,
        delay_options=[(n, delay_label(n)) for n in choices],
        retention=retention,
        retention_options=[(n, t(_RETENTION_LABELS.get(n, f"{n} days"))) for n in retention_choices],
        live_ints=[(key, label, getattr(settings, attr), await db.get_state(key) or "")
                   for key, (label, attr) in LIVE_INT_KEYS.items() if key not in _OWN_CONTROL],
        s=settings,
        models=sorted(await _model_ids() or []),
        restart=request.query_params.get("restart") == "1",
    )


@router.post("/settings/master")
async def save_master(request: Request):
    """Home-page switch: only the pause flag, so no other setting can be touched."""
    form = await request.form()
    await db.set_state_bool("paused", str(form.get("enabled", "")).strip() != "on")
    return back("/", msg="Saved. Applies to the next message.")


@router.post("/settings/live")
async def save_live(request: Request):
    form = await request.form()

    def field(key: str) -> str:
        return str(form.get(key, "")).strip()

    # A browser omits unchecked boxes. Only the main settings form (`_full`) means "absent = off";
    # any other post writes just the keys it contains, so a partial form can't reset the rest.
    full = field("_full") == "1"
    submitted: dict[str, str] = {}
    for key in ("approval_mode", "innercircle_gate"):
        if full or key in form:
            submitted[key] = _onoff(field(key) == "on")
    if "paused" in form:
        submitted["paused"] = _onoff(field("paused") == "on")
    elif full:
        submitted["paused"] = _onoff(field("enabled") != "on")
    quiet = (field("quiet_start"), field("quiet_end"))
    if field("quiet_on") == "off":
        quiet = ("", "")
    if any(quiet) and not all(_TIME_RE.match(t) for t in quiet):
        return back("/settings", err="Quiet hours need both a start and an end time.")
    if "quiet_start" in form or "quiet_end" in form or "quiet_on" in form:
        submitted["quiet_start"], submitted["quiet_end"] = quiet
    if "voice_override" in form:
        voice = field("voice_override")
        if voice not in ("", "on", "off"):
            return back("/settings", err="Unknown voice setting.")
        # An untouched switch posts the effective value; keep "" (follow the server default) then.
        if voice and voice == _onoff(settings.voice_transcribe) and not await db.get_state("voice_override"):
            voice = ""
        submitted["voice_override"] = voice
    for key, (label, _) in LIVE_INT_KEYS.items():
        if key not in form:
            continue
        raw = field(key)
        n = _to_int(raw) if raw else None
        if raw and (n is None or n < 0):
            return back("/settings", err=f"{t(label)} " + t("must be a whole number (blank = default)."))
        submitted[key] = "" if n is None else str(n)  # "" = env default, same as /delay off
    name = field("OWNER_FIRST_NAME")
    if "OWNER_FIRST_NAME" in form and not name:
        return back("/settings", err="OWNER_FIRST_NAME " + t("can't be empty."))
    # orig_* (optional): skip fields still equal to what the page loaded, so a stale tab
    # can't undo a /pause sent from the phone or wipe quiet hours the time input can't show.
    for key, value in submitted.items():
        if f"orig_{key}" not in form or value != field(f"orig_{key}"):
            await db.set_state(key, value)
    if name and name != settings.owner_first_name:
        try:
            setup.merge_env(request.app.state.env_path, setup.EXAMPLE_PATH, {"OWNER_FIRST_NAME": name})
        except OSError as e:
            return back("/settings", err=t("Couldn't write .env; nothing applied.") + f" ({e})")
        settings.owner_first_name = name
    return back("/settings", msg="Saved. Applies to the next message.")


@router.post("/privacy/wipe")
async def wipe(request: Request):
    """Delete every stored chat, memory, summary and draft. Contacts and settings stay."""
    form = await request.form()
    if str(form.get("confirm", "")).strip() != t("delete"):
        return back("/settings", err="To confirm, type the word exactly as shown.")
    await db.wipe_chats()
    return back("/settings", msg="All stored chats, memory and drafts are deleted.")


@router.post("/settings/config")
async def save_config(request: Request):
    form = await request.form()
    current = {
        "OPENROUTER_MODEL": settings.openrouter_model,
        "EXTRACTOR_MODEL": settings.extractor_model,
        "WHISPER_MODEL": settings.whisper_model,
        "OWNER_FIRST_NAME": settings.owner_first_name,
        "HISTORY_TURNS": str(settings.history_turns),
        "OWNER_USER_ID": str(settings.owner_user_id),
    }
    if settings.hosted:
        current.pop("OWNER_USER_ID")
    changes: dict[str, str] = {}
    for key, old in current.items():
        if key not in form:  # partial form (e.g. only the technical models): leave it as is
            continue
        new = str(form.get(key, "")).strip()
        if not new:
            return back("/settings", err=f"{key} " + t("can't be empty."))
        if key in ("HISTORY_TURNS", "OWNER_USER_ID"):
            # pydantic rejects non-ASCII digits at import: a raw "۸۰۱" in .env would
            # crash-loop the bot after restart, so store the ASCII form.
            n = _to_int(new)
            if n is None or n <= 0:
                return back("/settings", err=f"{key} " + t("must be a positive whole number."))
            new = str(n)
        if new != old:
            changes[key] = new
    if not 1 <= int(changes.get("HISTORY_TURNS", "1")) <= 100:
        return back("/settings", err="History turns must be between 1 and 100.")
    ids = await _model_ids()
    for key in CHECKED_MODEL_KEYS:
        if key in changes and ids is not None and changes[key] not in ids:
            return back("/settings", err=t("Unknown model id:") + f" {changes[key]}")
    if not settings.hosted:
        token = str(form.get("TG_BOT_TOKEN", "")).strip()
        api_key = str(form.get("OPENROUTER_API_KEY", "")).strip()
        try:
            if token and token != settings.tg_bot_token:
                await asyncio.to_thread(setup.tg_get_me, token)
                changes["TG_BOT_TOKEN"] = token
            if api_key and api_key != settings.openrouter_api_key:
                await asyncio.to_thread(setup.or_key_info, api_key)
                changes["OPENROUTER_API_KEY"] = api_key
        except setup.SetupError as e:
            return back("/settings", err=str(e))
        except httpx.TransportError as e:
            return back("/settings", err=t("Couldn't reach the service to check it; nothing saved.") + f" ({type(e).__name__})")
    if not changes:
        return back("/settings", msg="Nothing changed.")
    try:
        setup.merge_env(request.app.state.env_path, setup.EXAMPLE_PATH, changes)
    except OSError as e:
        return back("/settings", err=t("Couldn't write .env; nothing applied.") + f" ({e})")
    for key in LIVE_ENV_KEYS:
        if key in changes:
            setattr(settings, key.lower(), int(changes[key]) if key == "HISTORY_TURNS" else changes[key])
    if changes.keys() & RESTART_KEYS:
        return back("/settings", msg="Saved. Restart to apply the token, key or owner change.", restart="1")
    return back("/settings", msg="Saved and applied.")


@router.post("/restart")
async def restart(request: Request):
    request.app.state.request_stop()
    return render(request, "restarting.html")
