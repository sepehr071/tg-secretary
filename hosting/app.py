"""Hosting website: Telegram login, onboarding, account, admin, and the /app/ proxy."""
from __future__ import annotations

import asyncio
import logging
import secrets
import time
import unicodedata
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from fastapi import FastAPI, Form, Request
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import db, oidc, openrouter, pm2, tenants
from .config import settings

log = logging.getLogger(__name__)
_HERE = Path(__file__).resolve().parent
COOKIE = "hs_session"
templates = Jinja2Templates(directory=str(_HERE / "templates"))
PUBLIC_PATHS = {"/", "/login", "/privacy", oidc.REDIRECT_PATH, oidc.WIDGET_PATH}
MAX_AMOUNT = 1000.0
# Canned preview replies for the onboarding style step, keyed "tone-len-emoji" (same texts as the dashboard).
_REPLIES = [
    ["سلام، فردا عصر در دسترس نیستم. بعداً خبر می\u200cدهم.",
     "سلام، وقت بخیر. فردا تا ساعت شش جلسه دارم، ولی از هفت به بعد در خدمتم. اگر مناسب است همان موقع هماهنگ کنیم."],
    ["سلام! فردا تا شش سرم شلوغه، بعدش آزادم.",
     "سلام! فردا تا شش سر کارم، ولی از هفت به بعد آزادم. اگه برات خوبه همون موقع ببینیم، خبرم کن."],
    ["سلام! فردا تا شش گیرم، بعدش پایه\u200cام",
     "سلااام! فردا تا شش گیرم ولی از هفت به بعد کاملاً آزادم. بگو کجا بریم، من پایه\u200cام"],
]
_EMO = ["", " 🙂", " 😄✨"]
PREVIEWS = {f"{t}-{l}-{e}": _REPLIES[t][l] + _EMO[e] for t in range(3) for l in range(2) for e in range(3)}


def parse_amount(raw: str) -> float | None:
    """USD amount from an admin form; accepts Persian digits and the ٫ separator."""
    text = unicodedata.normalize("NFKC", raw.strip()).replace("٫", ".")
    try:
        value = float("".join(str(unicodedata.digit(ch)) if ch.isdigit() else ch for ch in text))
    except ValueError:
        return None
    return value if 0 < value <= MAX_AMOUNT else None


def _pick(raw: str, top: int) -> int | None:
    """A 0..top choice from a form radio; None when missing or out of range."""
    return int(raw) if raw.isascii() and raw.isdigit() and int(raw) <= top else None


async def next_step(tg_id: int) -> str:
    if not await db.has_consent(tg_id, settings.consent_version):
        return "consent"
    t = await db.get_tenant_by_owner(tg_id)
    if t is None or not t["profile_done"]:
        return "profile"
    if t["status"] in ("draft", "awaiting_credit"):
        return "payment"
    return "account"


STEP_URL = {"consent": "/onboard/consent", "profile": "/onboard/profile"}


def render(request: Request, name: str, status_code: int = 200, **ctx: Any):
    ctx.setdefault("user", getattr(request.state, "user", None))
    ctx.setdefault("msg", request.query_params.get("msg", ""))
    ctx.setdefault("err", request.query_params.get("err", ""))
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)


def go(url: str, **q: str) -> RedirectResponse:
    query = urlencode({k: v for k, v in q.items() if v})
    return RedirectResponse(f"{url}?{query}" if query else url, status_code=303)


def create_app() -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/hstatic", StaticFiles(directory=str(_HERE / "static")), name="hstatic")

    @app.middleware("http")
    async def guard(request: Request, call_next):
        if request.method == "POST" and request.headers.get("origin") != settings.public_url:
            return PlainTextResponse("bad origin", status_code=403)
        tg_id = await db.session_user(request.cookies.get(COOKIE))
        request.state.user = await db.get_user(tg_id) if tg_id else None
        path = request.url.path
        if request.state.user is None and path not in PUBLIC_PATHS and not path.startswith("/hstatic/"):
            if request.method == "GET":
                return RedirectResponse("/login", status_code=303)
            return PlainTextResponse("login required", status_code=401)
        if path.startswith("/admin") and request.state.user["tg_id"] != settings.admin_tg_id:
            return PlainTextResponse("forbidden", status_code=403)
        return await call_next(request)

    @app.get("/")
    async def index(request: Request):
        return render(request, "index.html")

    @app.get("/privacy")
    async def privacy(request: Request):
        # "۷" in the template = secretary.config.Settings.message_retention_days; keep them in step.
        return render(request, "privacy.html")

    async def _start_session(tg_id: int) -> RedirectResponse:
        resp = RedirectResponse("/account", status_code=303)
        resp.set_cookie(COOKIE, await db.create_session(tg_id), max_age=db.SESSION_TTL,
                        httponly=True, secure=True, samesite="lax", path="/")
        return resp

    @app.get("/login")
    async def login(request: Request):
        if not settings.oidc_client_id:
            return render(request, "login.html", bot=settings.platform_bot_username,
                          auth_url=settings.public_url + oidc.WIDGET_PATH)
        verifier, challenge = oidc.new_pkce()
        state = secrets.token_urlsafe(24)
        await db.put_oauth_state(state, verifier)
        return RedirectResponse(oidc.auth_url(state, challenge), status_code=303)

    @app.get(oidc.REDIRECT_PATH)
    async def callback(request: Request, code: str = "", state: str = ""):
        verifier = await db.pop_oauth_state(state) if state else None
        if not verifier or not code:
            return render(request, "error.html", 400, title="ورود انجام نشد",
                          body="لینک ورود منقضی شده؛ دوباره وارد شو.")
        try:
            claims = await oidc.exchange(code, verifier)
            tg_id = int(claims["id"])
            await db.upsert_user(tg_id, str(claims.get("name") or claims.get("given_name") or ""),
                                 claims.get("preferred_username"))
        except Exception:  # noqa: BLE001 - any failure here is "login failed" to the user
            log.exception("oidc exchange failed")
            return render(request, "error.html", 400, title="ورود انجام نشد",
                          body="دوباره امتحان کن.")
        return await _start_session(tg_id)

    @app.get(oidc.WIDGET_PATH)
    async def widget_login(request: Request):
        try:
            user = oidc.widget_user(dict(request.query_params), time.time())
            tg_id = int(user["id"])
            name = " ".join(p for p in (user.get("first_name"), user.get("last_name")) if p)
            await db.upsert_user(tg_id, name, user.get("username"))
        except ValueError:
            return render(request, "error.html", 400, title="ورود انجام نشد",
                          body="دوباره امتحان کن.")
        return await _start_session(tg_id)

    @app.post("/logout")
    async def logout(request: Request):
        await db.end_session(request.cookies.get(COOKIE))
        resp = RedirectResponse("/", status_code=303)
        resp.delete_cookie(COOKIE, path="/")
        return resp

    def uid(request: Request) -> int:
        return request.state.user["tg_id"]

    @app.get("/account")
    async def account(request: Request):
        step = await next_step(uid(request))
        if step in STEP_URL:
            return RedirectResponse(STEP_URL[step], status_code=303)
        t = await db.get_tenant_by_owner(uid(request))
        credit = None
        if t["or_key_hash"]:
            try:
                credit = await openrouter.get_key(t["or_key_hash"])
            except openrouter.OpenRouterError:
                log.warning("credit lookup failed for tenant %s", t["id"])
        return render(request, "account.html", tenant=t, credit=credit,
                      payment_instructions=settings.payment_instructions,
                      bot_username=settings.platform_bot_username,
                      connection=await tenants.connection_state(t["id"]))

    @app.get("/account/connected")
    async def account_connected(request: Request):
        t = await db.get_tenant_by_owner(uid(request))
        state = await tenants.connection_state(t["id"]) if t else "none"
        return JSONResponse({"connected": state != "none", "can_reply": state == "ok"})

    @app.get("/onboard/consent")
    async def consent_page(request: Request):
        return render(request, "consent.html", version=settings.consent_version)

    @app.post("/onboard/consent")
    async def consent(request: Request, accept: str = Form("")):
        if accept != "1":
            return go("/onboard/consent", err="برای ادامه باید شرایط رو بپذیری.")
        await db.add_consent(uid(request), settings.consent_version)
        return RedirectResponse("/account", status_code=303)

    async def _tenant_for(request: Request) -> dict:
        t = await db.get_tenant_by_owner(uid(request))
        if t is None:
            t = await db.get_tenant(await db.create_tenant(uid(request)))
        return t

    @app.get("/onboard/bot")
    async def bot_page():  # old bookmarks: the per-user bot step no longer exists
        return RedirectResponse("/account", status_code=303)

    @app.get("/onboard/profile")
    async def profile_page(request: Request):
        if await next_step(uid(request)) == "consent":
            return RedirectResponse("/onboard/consent", status_code=303)
        return render(request, "onboard_profile.html", first_name=request.state.user["first_name"] or "",
                      previews=PREVIEWS)

    @app.post("/onboard/profile")
    async def profile(request: Request, first_name: str = Form(""), about: str = Form(""),
                      style: str = Form(""), never: list[str] = Form([]), never_text: str = Form(""),
                      tone: str = Form(""), length: str = Form("", alias="len"), emoji: str = Form("")):
        if await next_step(uid(request)) == "consent":
            return RedirectResponse("/onboard/consent", status_code=303)
        t = await _tenant_for(request)
        # `never` carries the checkbox keys; an old client may post free text under the same name.
        keys = [v for v in never if v in tenants.NEVER_KEYS]
        legacy = " ".join(v.strip() for v in never if v not in tenants.NEVER_KEYS and v.strip())
        await tenants.save_profile(t["id"], first_name, about, style, " ".join(x for x in (never_text.strip(), legacy) if x),
                                   tone=_pick(tone, 2), length=_pick(length, 1), emoji=_pick(emoji, 2), never_keys=keys)
        if t["status"] not in ("draft", "awaiting_credit"):
            return RedirectResponse("/account", status_code=303)  # running/stopped: profile edit only
        if t["status"] == "draft":
            await db.update_tenant(t["id"], status="awaiting_credit")
        try:
            if settings.trial_credit_usd > 0:  # re-run every time: a failed earlier sync must not leave limit 0
                ref = f"trial-user-{t['owner_tg_id']}"
                await db.add_payment(t["id"], settings.trial_credit_usd, "trial", "", 0, ref)
                await tenants.sync_limit(t["id"])
                await db.mark_payment_applied(ref)
            t = await db.get_tenant(t["id"])
            if t["or_key_hash"]:  # credit exists (trial, or the admin paid before the profile was done)
                await tenants.activate(t["id"])
        except Exception as e:  # noqa: BLE001 - admin sees last_error and retries
            log.exception("activation failed")
            await db.update_tenant(t["id"], last_error=f"activate: {e}")
        return RedirectResponse("/account", status_code=303)

    @app.post("/account/delete")
    async def delete(request: Request, confirm: str = Form("")):
        if confirm.strip() != "حذف":
            return go("/account", err="برای تأیید، کلمه «حذف» رو بنویس.")
        t = await db.get_tenant_by_owner(uid(request))
        if t:
            await tenants.delete(t["id"])
        return go("/", msg="حساب و داده\u200cها پاک شد.")

    @app.get("/admin")
    async def admin(request: Request):
        procs = await asyncio.to_thread(pm2.status)
        rows = []
        for t in await db.list_tenants():
            user = await db.get_user(t["owner_tg_id"]) or {}
            info: dict = {}
            if t["or_key_hash"]:
                try:
                    info = await openrouter.get_key(t["or_key_hash"])
                except openrouter.OpenRouterError:
                    pass
            p = procs.get(pm2.name(t["id"]), {})
            rows.append({**t, "first_name": user.get("first_name"), "limit": info.get("limit"),
                         "usage": info.get("usage"), "pm2_status": p.get("status", "—"),
                         "restarts": p.get("restarts", 0), "client_ref": secrets.token_urlsafe(12)})
        return render(request, "admin.html", rows=rows)

    @app.post("/admin/payment")
    async def admin_payment(request: Request, tenant_id: str = Form(""), amount_usd: str = Form(""),
                            paid_text: str = Form(""), note: str = Form(""), client_ref: str = Form("")):
        amount = parse_amount(amount_usd)
        try:
            tenant_id = int(tenant_id)
        except ValueError:
            return go("/admin", err="این حساب معتبر نیست.")
        if amount is None or not client_ref:
            return go("/admin", err="مبلغ واردشده درست نیست.")
        try:
            added = await tenants.top_up(tenant_id, amount, paid_text, note, uid(request), client_ref)
        except ValueError:
            return go("/admin", err="این حساب هنوز آماده نیست؛ معرفی کامل نشده.")
        except Exception as e:  # noqa: BLE001 - show the failure, payment row is kept for retry
            log.exception("top-up failed")
            await db.update_tenant(tenant_id, last_error=f"top-up: {e}")
            return go("/admin", err=f"انجام نشد: {e}")
        return go("/admin", msg="ثبت شد." if added else "این پرداخت قبلاً ثبت شده بود.")

    @app.post("/admin/tenant/{tid}/restart")
    async def admin_restart(tid: int):
        t = await db.get_tenant(tid)
        if t is None or t["status"] not in ("running", "stopped"):
            return go("/admin", err="این حساب هنوز فعال نشده.")
        try:
            try:
                await asyncio.to_thread(pm2.restart, tid)  # pm2 restart also revives a stopped process
            except pm2.Pm2Error:
                await tenants.activate(tid)  # process missing from pm2: start it (reuses key, port, secret)
        except Exception as e:  # noqa: BLE001 - flash it, keep the status
            log.exception("restart failed")
            await db.update_tenant(tid, last_error=f"restart: {e}")
            return go("/admin", err=f"انجام نشد: {e}")
        await db.update_tenant(tid, status="running")
        return go("/admin", msg="انجام شد.")

    @app.post("/admin/tenant/{tid}/stop")
    async def admin_stop(tid: int):
        try:
            await asyncio.to_thread(pm2.stop, tid)
        except Exception as e:  # noqa: BLE001 - flash it, keep the status
            log.exception("stop failed")
            await db.update_tenant(tid, last_error=f"stop: {e}")
            return go("/admin", err=f"انجام نشد: {e}")
        await db.update_tenant(tid, status="stopped")
        return go("/admin", msg="متوقف شد.")

    from .proxy import router as proxy_router  # Task 10
    app.include_router(proxy_router)
    return app
