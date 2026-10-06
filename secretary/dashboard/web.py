"""Template + redirect helpers shared by the dashboard routes."""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from jinja2 import pass_context

from ..config import settings
from ..setup import mask
from .fa import FA

templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))
templates.env.filters["mask"] = mask
templates.env.filters["ts"] = lambda t: time.strftime("%Y-%m-%d %H:%M", time.localtime(t)) if t else "—"


def t(text: str) -> str:
    return FA.get(text, text) if settings.dashboard_lang == "fa" else text


# pass_context stops Jinja from folding "text"|t at compile time, which would freeze the language.
templates.env.filters["t"] = pass_context(lambda _ctx, text: t(text))


def render(request: Request, name: str, status_code: int = 200, **context: Any) -> HTMLResponse:
    # Read per request (not as Jinja globals) so a live settings change shows up.
    context.setdefault("hosted", settings.hosted)
    context.setdefault("lang", settings.dashboard_lang)
    context.setdefault("root", settings.dashboard_root_path)
    return templates.TemplateResponse(request, name, context, status_code=status_code)


def back(url: str, msg: str = "", err: str = "", **params: str) -> RedirectResponse:
    """Post-redirect-get with a one-line flash message in the query string."""
    url = settings.dashboard_root_path + url
    msg, err = t(msg), t(err)  # fixed texts only; callers translate any dynamic part
    query = urlencode({k: v for k, v in {"msg": msg, "err": err, **params}.items() if v})
    return RedirectResponse(f"{url}?{query}" if query else url, status_code=303)
