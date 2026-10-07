"""/app/* -> the tenant's own dashboard on 127.0.0.1:<port>, authenticated by X-Platform-Auth."""
from __future__ import annotations

import posixpath
from urllib.parse import unquote

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, Response

from . import db

router = APIRouter()
TRANSPORT: httpx.AsyncBaseTransport | None = None
_PASS_REQ = ("content-type", "accept")  # never the browser Cookie: the platform session stays here
_PASS_RESP = ("content-type", "location", "cache-control")  # never Set-Cookie


@router.get("/app")
async def proxy_root():
    return RedirectResponse("/app/", status_code=303)


@router.api_route("/app/{path:path}", methods=["GET", "POST"])
async def proxy(path: str, request: Request):
    from .app import render  # late import: app imports this module
    if posixpath.normpath("/" + unquote(path)).startswith("/_tg"):
        return Response("not found", 404)  # update intake is router-only
    t = await db.get_tenant_by_owner(request.state.user["tg_id"])
    if t is None or t["status"] != "running" or not t["dashboard_port"] or not t["proxy_secret"]:
        return RedirectResponse("/account", status_code=303)
    headers = {k: v for k in _PASS_REQ if (v := request.headers.get(k))}
    headers["x-platform-auth"] = t["proxy_secret"]
    try:
        async with httpx.AsyncClient(transport=TRANSPORT, timeout=60) as c:
            r = await c.request(request.method, f"http://127.0.0.1:{t['dashboard_port']}/{path}",
                                params=request.query_params, content=await request.body(),
                                headers=headers, follow_redirects=False)
    except httpx.TransportError:
        return render(request, "error.html", 503, title="منشی در حال آماده شدنه",
                      body="چند ثانیه دیگه صفحه رو دوباره باز کن.")
    return Response(r.content, r.status_code, {k: v for k in _PASS_RESP if (v := r.headers.get(k))})
