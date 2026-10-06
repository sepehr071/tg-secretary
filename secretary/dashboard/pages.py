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
from .web import back, render, t

router = APIRouter()

_RIGHTS = (("can_reply", "reply"), ("can_read_messages", "read messages"))


def _duration(seconds: float) -> str:
    minutes = int(seconds // 60)
    days, minutes = divmod(minutes, 1440)
    hours, minutes = divmod(minutes, 60)
    return f"{days}d {hours}h" if days else f"{hours}h {minutes}m"


@router.get("/")
async def status(request: Request):
    try:
        credit = await asyncio.to_thread(setup.or_key_info, settings.openrouter_api_key, 5)
    except Exception:  # noqa: BLE001 — credit is informational; never break the page
        credit = None
    connections = await db.list_connections()
    for c in connections:
        rights = json.loads(c.get("rights_json") or "{}")
        c["missing"] = [label for key, label in _RIGHTS if not rights.get(key)]
    return render(
        request, "status.html",
        bot_username=request.app.state.bot.username,
        uptime=_duration(time.time() - request.app.state.started_at),
        model=settings.openrouter_model,
        paused=await db.get_state_bool("paused"),
        approval=await db.get_state_bool("approval_mode"),
        innercircle=await db.get_state_bool("innercircle_gate", default=True),
        credit=credit,
        connections=connections,
        stats=await db.get_stats(),
        replies=await db.recent_bot_replies(10),
    )


LIVE_INT_KEYS = {
    "delay_override": ("Reply delay (s)", "auto_reply_delay_seconds"),
    "away_delay_override": ("Away-mode delay (s)", "away_reply_delay_seconds"),
    "cooldown_override": ("Owner-active cooldown (s)", "owner_active_cooldown_seconds"),
}
# .env keys the bot reads per call, so assigning them on `settings` applies at once.
LIVE_ENV_KEYS = ("OPENROUTER_MODEL", "EXTRACTOR_MODEL", "WHISPER_MODEL", "OWNER_FIRST_NAME", "HISTORY_TURNS")
RESTART_KEYS = {"TG_BOT_TOKEN", "OPENROUTER_API_KEY", "OWNER_USER_ID"}
# Whisper is an audio model and may be missing from /models, so only these are checked.
CHECKED_MODEL_KEYS = ("OPENROUTER_MODEL", "EXTRACTOR_MODEL")
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_models_cache: tuple[float, set[str] | None] = (0.0, None)


async def _model_ids() -> set[str] | None:
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
    # Rendered twice: as the visible inputs and as hidden orig_* fields, so a save
    # writes only what the user changed on this page.
    live = {
        "paused": _onoff(await db.get_state_bool("paused")),
        "approval_mode": _onoff(await db.get_state_bool("approval_mode")),
        "innercircle_gate": _onoff(await db.get_state_bool("innercircle_gate", default=True)),
        "voice_override": await db.get_state("voice_override") or "",
        "quiet_start": _pad_time(await db.get_state("quiet_start") or ""),
        "quiet_end": _pad_time(await db.get_state("quiet_end") or ""),
        **{key: await db.get_state(key) or "" for key in LIVE_INT_KEYS},
    }
    return render(
        request, "settings.html",
        live=live,
        live_ints=[(key, label, getattr(settings, attr)) for key, (label, attr) in LIVE_INT_KEYS.items()],
        s=settings,
        models=sorted(await _model_ids() or []),
        restart=request.query_params.get("restart") == "1",
    )


@router.post("/settings/live")
async def save_live(request: Request):
    form = await request.form()

    def field(key: str) -> str:
        return str(form.get(key, "")).strip()

    quiet = (field("quiet_start"), field("quiet_end"))
    if any(quiet) and not all(_TIME_RE.match(t) for t in quiet):
        return back("/settings", err="Quiet hours need both a start and an end time.")
    submitted = {
        "paused": _onoff(field("paused") == "on"),
        "approval_mode": _onoff(field("approval_mode") == "on"),
        "innercircle_gate": _onoff(field("innercircle_gate") == "on"),
        "voice_override": field("voice_override"),
        "quiet_start": quiet[0],
        "quiet_end": quiet[1],
    }
    if submitted["voice_override"] not in ("", "on", "off"):
        return back("/settings", err="Unknown voice setting.")
    for key, (label, _) in LIVE_INT_KEYS.items():
        raw = field(key)
        n = _to_int(raw) if raw else None
        if raw and (n is None or n < 0):
            return back("/settings", err=f"{t(label)} " + t("must be a whole number (blank = default)."))
        submitted[key] = "" if n is None else str(n)  # "" = env default, same as /delay off
    # Only what changed on this page: a stale tab must not undo a /pause sent from the
    # phone, nor wipe quiet hours the time input couldn't display.
    for key, value in submitted.items():
        if value != field(f"orig_{key}"):
            await db.set_state(key, value)
    return back("/settings", msg="Saved. Applies to the next message.")


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
