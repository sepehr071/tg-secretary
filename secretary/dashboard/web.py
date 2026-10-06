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

_FA_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")
_JALALI_MONTHS = ("فروردین", "اردیبهشت", "خرداد", "تیر", "مرداد", "شهریور",
                  "مهر", "آبان", "آذر", "دی", "بهمن", "اسفند")


def jalali(gy: int, gm: int, gd: int) -> tuple[int, int, int]:
    """Gregorian -> Jalali (Shamsi) date; the arithmetic from the public-domain jdf.scr.ir."""
    gy2 = gy + 1 if gm > 2 else gy
    days = (355666 + 365 * gy + (gy2 + 3) // 4 - (gy2 + 99) // 100 + (gy2 + 399) // 400 + gd
            + (0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334)[gm - 1])
    jy = -1595 + 33 * (days // 12053)
    days %= 12053
    jy += 4 * (days // 1461)
    days %= 1461
    if days > 365:
        jy += (days - 1) // 365
        days = (days - 1) % 365
    if days < 186:
        return jy, 1 + days // 31, 1 + days % 31
    return jy, 7 + (days - 186) // 30, 1 + (days - 186) % 30


# Nowruz 1403 (leap year before it), a Bahman date, and Esfand 30 of leap 1403.
assert jalali(2024, 3, 20) == (1403, 1, 1) and jalali(1979, 2, 11) == (1357, 11, 22)
assert jalali(2025, 3, 20) == (1403, 12, 30) and jalali(2026, 10, 6) == (1405, 7, 14)


def fa_digits(v: Any) -> str:
    return str(v).translate(_FA_DIGITS) if settings.dashboard_lang == "fa" else str(v)


def ts(stamp: float | None) -> str:
    if not stamp:
        return "—"
    lt = time.localtime(stamp)
    if settings.dashboard_lang != "fa":
        return time.strftime("%Y-%m-%d %H:%M", lt)
    jy, jm, jd = jalali(lt.tm_year, lt.tm_mon, lt.tm_mday)
    return fa_digits(f"{jd} {_JALALI_MONTHS[jm - 1]} {jy}، {lt.tm_hour:02d}:{lt.tm_min:02d}")


def t(text: str) -> str:
    return FA.get(text, text) if settings.dashboard_lang == "fa" else text


# pass_context stops Jinja from folding "text"|t at compile time, which would freeze the language.
templates.env.filters["t"] = pass_context(lambda _ctx, text: t(text))
templates.env.filters["num"] = pass_context(lambda _ctx, v: fa_digits(v))
templates.env.filters["ts"] = pass_context(lambda _ctx, v: ts(v))


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
