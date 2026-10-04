"""Template + redirect helpers shared by the dashboard routes."""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from ..setup import mask

templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))
templates.env.filters["mask"] = mask
templates.env.filters["ts"] = lambda t: time.strftime("%Y-%m-%d %H:%M", time.localtime(t)) if t else "—"


def render(request: Request, name: str, status_code: int = 200, **context: Any) -> HTMLResponse:
    return templates.TemplateResponse(request, name, context, status_code=status_code)


def back(url: str, msg: str = "", err: str = "", **params: str) -> RedirectResponse:
    """Post-redirect-get with a one-line flash message in the query string."""
    query = urlencode({k: v for k, v in {"msg": msg, "err": err, **params}.items() if v})
    return RedirectResponse(f"{url}?{query}" if query else url, status_code=303)
