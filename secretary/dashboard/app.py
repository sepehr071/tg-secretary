"""Owner dashboard: a FastAPI app served inside the bot process on 127.0.0.1."""
from __future__ import annotations

import contextlib
import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, Form, Request
from fastapi.responses import PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from .. import db
from ..config import settings
from ..setup import ENV_PATH
from . import auth, contacts, drafts, pages
from .web import render

log = logging.getLogger(__name__)
_HERE = Path(__file__).resolve().parent


def create_app(bot: Any, request_stop: Callable[[], None], env_path: Path = ENV_PATH) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.bot = bot
    app.state.request_stop = request_stop
    app.state.env_path = env_path
    app.state.started_at = time.time()
    app.mount("/static", StaticFiles(directory=str(_HERE / "static")), name="static")

    @app.middleware("http")
    async def guard(request: Request, call_next):
        if settings.hosted:
            # Only the hosting proxy may talk to us; it already checked the user's
            # session and Origin, so Host/Origin/login checks don't apply here.
            if not auth.proxy_ok(request.headers.get("x-platform-auth")):
                return PlainTextResponse("forbidden", status_code=403)
            request.state.pending = await db.count_open_pending()
            return await call_next(request)
        host = request.headers.get("host", "")
        if not auth.host_ok(host):
            return PlainTextResponse("bad host", status_code=400)
        if request.method == "POST" and not auth.origin_ok(request.headers.get("origin"), host):
            return PlainTextResponse("bad origin", status_code=403)
        path = request.url.path
        if path != "/login" and not path.startswith("/static/"):
            if not await auth.session_valid(request.cookies.get(auth.COOKIE)):
                if request.method == "GET":
                    return RedirectResponse("/login", status_code=303)
                return PlainTextResponse("login required", status_code=401)
            request.state.pending = await db.count_open_pending()
        return await call_next(request)

    @app.get("/login")
    async def login_page(request: Request):
        return render(request, "login.html")

    @app.post("/login")
    async def login(request: Request, token: str = Form("")):
        session = await auth.redeem_login_token(token.strip())
        if session is None:
            return render(
                request, "login.html", status_code=401,
                err="That link is invalid, used or expired. Send /dashboard to your bot for a new one.",
            )
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie(auth.COOKIE, session, max_age=auth.SESSION_TTL, httponly=True,
                        samesite="strict", path="/",
                        secure=settings.dashboard_public_url.startswith("https://"))
        return resp

    @app.post("/logout")
    async def logout(request: Request):
        await auth.end_session(request.cookies.get(auth.COOKIE))
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie(auth.COOKIE, path="/")
        return resp

    app.include_router(pages.router)
    app.include_router(contacts.router)
    app.include_router(drafts.router)
    return app


class _Server(uvicorn.Server):
    def capture_signals(self):  # type: ignore[override]
        # The bot owns SIGINT/SIGTERM via loop.add_signal_handler; uvicorn must not replace them.
        return contextlib.nullcontext()


def make_server(app: FastAPI, port: int, host: str = "127.0.0.1") -> uvicorn.Server:
    config = uvicorn.Config(app, host=host, port=port, access_log=False,
                            log_config=None, log_level="warning")
    return _Server(config)


async def run_server(server: uvicorn.Server) -> None:
    """Serve until server.should_exit. A busy port logs an error instead of killing the bot."""
    try:
        await server.serve()
    except SystemExit:  # uvicorn calls sys.exit(1) when it can't bind
        log.error("dashboard not started: port %s is busy; the bot keeps running", server.config.port)
