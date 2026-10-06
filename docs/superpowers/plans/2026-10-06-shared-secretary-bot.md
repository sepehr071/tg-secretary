# Shared Secretary Bot (Revision 2) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace per-user bots with one shared secretary bot: the hosting process polls it, routes every update to the right tenant process over loopback, and onboarding loses its bot step.

**Architecture:** `hosting/bot.py` is the only poller of the platform bot. A new `hosting/router.py` maps each update to a running tenant (by user id or business connection id) and POSTs the raw JSON to `127.0.0.1:<dashboard_port>/_tg/update` with `X-Platform-Auth`. In hosted mode the tenant (`secretary/`) never polls; its dashboard app pushes received updates onto the PTB `update_queue`.

**Tech Stack:** unchanged (Python 3.13+, python-telegram-bot 22, FastAPI, httpx, aiosqlite).

**Spec:** `docs/superpowers/specs/2026-10-06-hosted-platform-design.md`, section "Revision 2". The original plan `docs/superpowers/plans/2026-10-06-hosted-platform.md` Global Constraints still bind.

## Global Constraints

- All Global Constraints of the original plan still apply (package `hosting`, `\u200c` escapes in .py / `&zwnj;` in templates, Edit/Write only, offline plain-assert smoke tests, no new deps, no Claude attribution in commits).
- Forward endpoint (exact): `POST /_tg/update` on the tenant dashboard, header `X-Platform-Auth`, body = raw Telegram update JSON. Hosted mode only.
- `allowed_updates` (exact): `["message", "callback_query", "business_connection", "business_message", "edited_business_message", "deleted_business_messages"]`.
- Only tenants with status `running` receive updates. Everything else is dropped; a `business_connection` from an account with no running tenant gets one DM with the site link (once per Telegram user id).
- Tenant `TG_BOT_TOKEN` = the platform bot token.
- Never log update contents or tokens; log tenant id + update type only.

## Review Focus

1. **Stranger attaches the shared bot** (no tenant, or tenant not running): no forward, ever; one DM at most. Pinned in Task 2.
2. **Business message whose connection id is unknown** (connection made before the platform restarted): resolved via `getBusinessConnection`, then cached. Pinned in Task 2.
3. **Tenant process down** when an update arrives: forward fails fast (10 s timeout), loop continues, next update still routed. Pinned in Task 2.
4. **Owner DM commands** (`/pause`, HITL buttons) reach only the sender's own tenant. Pinned in Task 2.
5. **Returning user mid-onboarding** after the bot step is removed: `next_step` never returns `"bot"`; old `/onboard/bot` URL redirects to `/account`. Pinned in Task 3.

## Waves
- Wave 1 (parallel, disjoint): Task 1 (`secretary/`, `scripts/smoke_dashboard.py`) ∥ Task 2 (`hosting/db.py`, `hosting/router.py`, `hosting/bot.py`, `scripts/smoke_hosting.py`).
- Wave 2: Task 3 (`hosting/app.py`, `hosting/tenants.py`, `hosting/templates/*`, `hosting/static/app.js`, `scripts/smoke_hosting.py`).
- Then: deploy to personal-france staging and re-run the end-to-end test.

---

### Task 1: Tenant receives updates over loopback (hosted mode)

**Files:**
- Modify: `secretary/__main__.py` (Application build + polling start, ~lines 37-90; `create_app` call ~111)
- Modify: `secretary/dashboard/app.py` (`create_app` signature, new route)
- Test: `scripts/smoke_dashboard.py`

**Interfaces:**
- Produces: `create_app(bot, request_stop, env_path=ENV_PATH, update_queue: asyncio.Queue | None = None)`; route `POST /_tg/update`.

- [ ] **Step 1: Failing test** (add to smoke_dashboard.py, call from runner):

```python
async def check_update_intake() -> None:
    import asyncio as _a
    from telegram import Update
    q: _a.Queue = _a.Queue()
    upd = {"update_id": 77, "message": {"message_id": 1, "date": 0,
           "chat": {"id": 111, "type": "private"}, "from": {"id": 111, "is_bot": False, "first_name": "O"},
           "text": "/pause"}}
    old = (settings.hosted, settings.dashboard_proxy_secret)
    settings.hosted, settings.dashboard_proxy_secret = True, "p"
    try:
        app = create_app(BOT, lambda: None, env_path=ENV, update_queue=q)
        async with client(app) as c:
            assert (await c.post("/_tg/update", json=upd)).status_code == 403
            r = await c.post("/_tg/update", json=upd, headers={"x-platform-auth": "p"})
            assert r.status_code == 200, r.status_code
        got = q.get_nowait()
        assert isinstance(got, Update) and got.update_id == 77 and got.message.text == "/pause"
    finally:
        settings.hosted, settings.dashboard_proxy_secret = old
    # Not hosted: the endpoint does not exist.
    app = create_app(BOT, lambda: None, env_path=ENV, update_queue=q)
    async with client(app) as c:
        r = await c.post("/_tg/update", json=upd, headers=ORIGIN)
        assert r.status_code in (401, 404), r.status_code
```

`BOT` (FakeBot) must work as the `bot` argument of `Update.de_json`; if PTB needs a real `telegram.Bot`, build the Update with `Update.de_json(data, None)` instead and note it.

- [ ] **Step 2: Run, verify FAIL** — `PYTHONIOENCODING=utf-8 uv run python scripts/smoke_dashboard.py` → `TypeError: create_app() got an unexpected keyword argument 'update_queue'`.

- [ ] **Step 3: Implement**

`secretary/dashboard/app.py`:

```python
def create_app(bot: Any, request_stop: Callable[[], None], env_path: Path = ENV_PATH,
               update_queue: "asyncio.Queue | None" = None) -> FastAPI:
    ...
    app.state.update_queue = update_queue
    ...
    if settings.hosted:
        @app.post("/_tg/update")
        async def tg_update(request: Request):
            # The hosting router is the only poller of the shared bot; it pushes
            # this tenant's updates here (guard above already checked X-Platform-Auth).
            data = await request.json()
            await request.app.state.update_queue.put(Update.de_json(data, request.app.state.bot))
            return PlainTextResponse("ok")
```

(import `asyncio` and `from telegram import Update`; register the route before `include_router` calls.)

`secretary/__main__.py`:

```python
    builder = Application.builder().token(settings.tg_bot_token).concurrent_updates(True)
    if settings.hosted:
        builder = builder.updater(None)  # hosting router feeds updates via /_tg/update
    app = builder.build()
    ...
    await app.initialize()
    await app.start()
    if not settings.hosted:
        assert app.updater is not None
        await app.updater.start_polling(...unchanged...)
    ...
        dash_server = make_server(create_app(app.bot, _request_stop, update_queue=app.update_queue), ...)
```

Check the shutdown path: wherever `app.updater.stop()` is called, guard it with `if app.updater is not None` (or `app.updater and app.updater.running`).

In hosted mode the dashboard must be enabled; if `settings.hosted and not settings.dashboard_enabled`, log an error at startup (updates can't arrive).

- [ ] **Step 4: Run all** — `PYTHONIOENCODING=utf-8 uv run python scripts/smoke_dashboard.py && uv run python scripts/smoke_core.py && uv run python scripts/smoke_setup.py && TG_BOT_TOKEN=1:x OPENROUTER_API_KEY=k OWNER_USER_ID=1 uv run python -c "import secretary.__main__; print('OK')"` → all pass.

- [ ] **Step 5: Commit** — `feat(secretary): hosted mode receives updates from the router`.

---

### Task 2: Router — route and forward shared-bot updates

**Files:**
- Modify: `hosting/db.py` (schema + helpers)
- Create: `hosting/router.py`
- Modify: `hosting/bot.py` (poll loop, handle_update; remove managed-bot code)
- Test: `scripts/smoke_hosting.py`

**Interfaces:**
- Produces (db): `put_biz_conn(conn_id: str, owner_tg_id: int)`, `get_biz_conn_owner(conn_id) -> int | None`, `mark_stranger_notified(tg_id) -> bool` (True the first time only).
- Produces (router): `TRANSPORT` (httpx, tests swap), `async owner_of(u: dict) -> int | None`, `async forward(tenant: dict, u: dict) -> bool`, `async dispatch(u: dict) -> str` returning one of `"forwarded" | "dropped" | "platform"` (for tests).
- Produces (bot): `ALLOWED_UPDATES` constant (exact list above); `handle_update(u)` delegates to `router.dispatch` and handles the `"platform"` case.

- [ ] **Step 1: Failing tests** (append above `main()`; remove or rewrite the old `check_platform_bot` managed-bot assertions and `check_managed_attach_route` — managed bots are gone; keep its credit-warning assertions as a separate check):

```python
from hosting import router  # noqa: E402


@check
async def check_router() -> None:
    sent_tg, fwd = [], []
    tg.TRANSPORT = httpx.MockTransport(lambda r: (
        sent_tg.append((r.url.path.rsplit("/", 1)[1], json.loads(r.content or b"{}"))),
        httpx.Response(200, json={"ok": True, "result":
            {"id": "c-late", "user": {"id": 81}, "user_chat_id": 81, "date": 0, "can_reply": True, "is_enabled": True}
            if r.url.path.endswith("getBusinessConnection") else True}))[1])

    def upstream(req: httpx.Request) -> httpx.Response:
        fwd.append((req.url.port, req.headers.get("x-platform-auth"), json.loads(req.content)))
        return httpx.Response(200, text="ok")
    router.TRANSPORT = httpx.MockTransport(upstream)

    await db.upsert_user(81, "Run", None)
    tid = await db.create_tenant(81)
    await db.update_tenant(tid, status="running", proxy_secret="s81")
    port = await db.alloc_port(tid)

    conn = {"update_id": 1, "business_connection": {"id": "c81", "user": {"id": 81}, "user_chat_id": 81,
            "date": 0, "can_reply": True, "is_enabled": True}}
    assert await router.dispatch(conn) == "forwarded"
    assert fwd[-1] == (port, "s81", conn)
    msg = {"update_id": 2, "business_message": {"message_id": 5, "date": 0, "business_connection_id": "c81",
           "chat": {"id": 900, "type": "private"}, "from": {"id": 900, "is_bot": False, "first_name": "F"}, "text": "hi"}}
    assert await router.dispatch(msg) == "forwarded" and fwd[-1][2]["update_id"] == 2
    # Unknown connection id: resolved via getBusinessConnection, then cached.
    late = {"update_id": 3, "deleted_business_messages": {"business_connection_id": "c-late",
            "chat": {"id": 900, "type": "private"}, "message_ids": [5]}}
    assert await router.dispatch(late) == "forwarded"
    n = len([m for m, _ in sent_tg if m == "getBusinessConnection"])
    assert await router.dispatch({**late, "update_id": 4}) == "forwarded"
    assert len([m for m, _ in sent_tg if m == "getBusinessConnection"]) == n   # cached
    # Owner DM and button go to the owner's tenant.
    dm = {"update_id": 5, "message": {"message_id": 9, "date": 0, "chat": {"id": 81, "type": "private"},
          "from": {"id": 81, "is_bot": False, "first_name": "Run"}, "text": "/pause"}}
    assert await router.dispatch(dm) == "forwarded"
    cb = {"update_id": 6, "callback_query": {"id": "q", "chat_instance": "x", "from": {"id": 81, "is_bot": False, "first_name": "Run"}, "data": "a"}}
    assert await router.dispatch(cb) == "forwarded"

    # Stranger: never forwarded; one DM only.
    before = len(fwd)
    s_conn = {"update_id": 7, "business_connection": {"id": "c99", "user": {"id": 99}, "user_chat_id": 99,
              "date": 0, "can_reply": True, "is_enabled": True}}
    assert await router.dispatch(s_conn) == "dropped"
    assert await router.dispatch({**s_conn, "update_id": 8}) == "dropped"
    s_msg = {**msg, "update_id": 9, "business_message": {**msg["business_message"], "business_connection_id": "c99"}}
    assert await router.dispatch(s_msg) == "dropped"
    assert len(fwd) == before
    assert [b["chat_id"] for m, b in sent_tg if m == "sendMessage"] == [99]
    # Stranger /start goes to the platform handler.
    s_dm = {**dm, "update_id": 10, "message": {**dm["message"], "from": {"id": 99, "is_bot": False, "first_name": "S"},
            "chat": {"id": 99, "type": "private"}, "text": "/start"}}
    assert await router.dispatch(s_dm) == "platform"

    # Tenant not running: dropped.
    await db.update_tenant(tid, status="stopped")
    assert await router.dispatch({**msg, "update_id": 11}) == "dropped"
    await db.update_tenant(tid, status="running")

    # Tenant down: forward fails, returns quickly, next update still routed.
    def down(req): raise httpx.ConnectError("refused")
    router.TRANSPORT = httpx.MockTransport(down)
    assert await router.dispatch({**msg, "update_id": 12}) == "dropped"
    router.TRANSPORT = httpx.MockTransport(upstream)
    assert await router.dispatch({**msg, "update_id": 13}) == "forwarded"
    tg.TRANSPORT = None
    router.TRANSPORT = None
```

- [ ] **Step 2: Run, verify FAIL** — `ImportError: cannot import name 'router'`.

- [ ] **Step 3: Implement**

`hosting/db.py` schema additions:

```sql
CREATE TABLE IF NOT EXISTS biz_connections (conn_id TEXT PRIMARY KEY, owner_tg_id INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS notified_strangers (tg_id INTEGER PRIMARY KEY);
```

```python
async def put_biz_conn(conn_id: str, owner_tg_id: int) -> None:
    await _write("INSERT INTO biz_connections VALUES (?, ?) ON CONFLICT(conn_id) DO UPDATE SET owner_tg_id=excluded.owner_tg_id",
                 conn_id, owner_tg_id)


async def get_biz_conn_owner(conn_id: str) -> int | None:
    row = await _one("SELECT owner_tg_id FROM biz_connections WHERE conn_id=?", conn_id)
    return row["owner_tg_id"] if row else None


async def mark_stranger_notified(tg_id: int) -> bool:
    try:
        await _write("INSERT INTO notified_strangers VALUES (?)", tg_id)
    except sqlite3.IntegrityError:
        return False
    return True
```

`hosting/router.py`:

```python
"""Route shared-bot updates to the owning tenant's process (127.0.0.1:<port>/_tg/update)."""
from __future__ import annotations

import logging

import httpx

from . import db, tg
from .config import settings

log = logging.getLogger(__name__)
TRANSPORT: httpx.AsyncBaseTransport | None = None
_BIZ_KEYS = ("business_message", "edited_business_message", "deleted_business_messages")


async def owner_of(u: dict) -> int | None:
    """Telegram user id whose tenant should get this update, or None."""
    if bc := u.get("business_connection"):
        await db.put_biz_conn(bc["id"], bc["user"]["id"])
        return bc["user"]["id"]
    for key in _BIZ_KEYS:
        if body := u.get(key):
            conn_id = body.get("business_connection_id")
            if not conn_id:
                return None
            owner = await db.get_biz_conn_owner(conn_id)
            if owner is None:
                try:
                    bc = await tg.call(settings.platform_bot_token, "getBusinessConnection",
                                       business_connection_id=conn_id)
                except tg.TelegramError as e:
                    log.warning("getBusinessConnection failed: %s", e)
                    return None
                owner = bc["user"]["id"]
                await db.put_biz_conn(conn_id, owner)
            return owner
    for key in ("message", "callback_query"):
        if body := u.get(key):
            return (body.get("from") or {}).get("id")
    return None


async def forward(t: dict, u: dict) -> bool:
    try:
        async with httpx.AsyncClient(transport=TRANSPORT, timeout=10) as c:
            r = await c.post(f"http://127.0.0.1:{t['dashboard_port']}/_tg/update", json=u,
                             headers={"x-platform-auth": t["proxy_secret"]})
    except httpx.HTTPError as e:
        log.warning("forward to tenant %s failed: %s", t["id"], type(e).__name__)
        return False
    if r.status_code >= 300:
        log.warning("tenant %s refused update: HTTP %s", t["id"], r.status_code)
        return False
    return True


async def dispatch(u: dict) -> str:
    owner = await owner_of(u)
    t = await db.get_tenant_by_owner(owner) if owner else None
    if t and t["status"] == "running" and t["dashboard_port"] and t["proxy_secret"]:
        return "forwarded" if await forward(t, u) else "dropped"
    if "message" in u:
        return "platform"
    if (bc := u.get("business_connection")) and await db.mark_stranger_notified(bc["user"]["id"]):
        try:
            await tg.call(settings.platform_bot_token, "sendMessage", chat_id=bc["user"]["id"],
                          text=f"برای فعال\u200cکردن منشی، اول در سایت ثبت\u200cنام کنید:\n{settings.public_url}")
        except tg.TelegramError as e:
            log.warning("stranger DM failed: %s", e)
    return "dropped"
```

Note: a stranger's DM `message` returns `"platform"` (the platform answers `/start`); a running owner's DM goes to their tenant (their tenant's own `/start` handler answers).

`hosting/bot.py`: delete `on_managed_bot` and the `managed_bot` branch; add

```python
ALLOWED_UPDATES = ["message", "callback_query", "business_connection", "business_message",
                   "edited_business_message", "deleted_business_messages"]


async def handle_update(u: dict) -> None:
    if await router.dispatch(u) != "platform":
        return
    msg = u.get("message") or {}
    if (msg.get("text") or "").startswith("/start"):
        await _say(msg["chat"]["id"], f"برای ساخت منشی خودتان وارد سایت شوید:\n{settings.public_url}")
```

and use `allowed_updates=ALLOWED_UPDATES` in `getUpdates`. Update the module docstring. Remove the now-unused `tenants` import if unused.

- [ ] **Step 4: Run** — `PYTHONIOENCODING=utf-8 uv run python scripts/smoke_hosting.py` all ok; `uv run python scripts/smoke_core.py`.
- [ ] **Step 5: Commit** — `feat(hosting): route shared-bot updates to tenant processes`.

---

### Task 3: Onboarding without a bot step

**Files:**
- Modify: `hosting/app.py`, `hosting/tenants.py`
- Delete: `hosting/templates/onboard_bot.html`
- Modify: `hosting/templates/account.html`, `hosting/templates/admin.html`, `hosting/templates/index.html` (any "create a bot" copy), `hosting/templates/onboard_profile.html` / `consent.html` / account payment section (step counters "از ۴" → "از ۳"), `hosting/static/app.js`
- Test: `scripts/smoke_hosting.py`

**Interfaces:**
- Consumes: Task 2's routing (nothing to call).
- Produces: `next_step()` returns `"consent" | "profile" | "payment" | "account"`; `tenants.save_profile` also writes the base tenant `.env` (moved from `attach_bot`); `tenants.attach_bot` removed; `top_up` requires `profile_done`.

- [ ] **Step 1: Failing tests** — rewrite `check_routes` onboarding part and add:

```python
@check
async def check_onboarding_v2() -> None:
    app = happ.create_app()
    await db.upsert_user(120, "Nil", "nil")
    cookie = await db.create_session(120)
    async with web(app, cookie) as c:
        assert await happ.next_step(120) == "consent"
        await c.post("/onboard/consent", data={"accept": "1"})
        assert await happ.next_step(120) == "profile"
        r = await c.get("/onboard/bot")                       # old URL
        assert r.status_code == 303 and r.headers["location"] == "/account"
        await c.post("/onboard/profile", data={"first_name": "Nil", "about": "x", "style": "", "never": ""})
        assert await happ.next_step(120) == "payment"
        t = await db.get_tenant_by_owner(120)
        env = (tenants.tenant_dir(t["id"]) / ".env").read_text(encoding="utf-8")
        for k in ("TG_BOT_TOKEN='1:platform'", "OWNER_USER_ID='120'", "HOSTED='1'",
                  "DASHBOARD_ROOT_PATH='/app'", "DASHBOARD_LANG='fa'"):
            assert k in env, k
        r = await c.get("/account")
        assert "@plat_bot" in r.text and "Chat Automation" in r.text
    assert not hasattr(tenants, "attach_bot")
```

Adjust every existing check that used `attach_bot`, `/onboard/bot/token`, `/onboard/bot/status`, `BotTaken`, managed bots, or `bot_id` as a readiness gate (top_up readiness is now `profile_done`). Keep their intent (e.g. the "two users claim the same bot" check is obsolete — delete it; the "unknown/not-ready tenant top_up raises ValueError" check now uses a tenant with `profile_done=0`).

- [ ] **Step 2: Run, verify FAIL.**

- [ ] **Step 3: Implement**
  - `next_step`: drop the `bot` branch; `STEP_URL` drops `"bot"`.
  - Remove routes `/onboard/bot`, `/onboard/bot/token`, `/onboard/bot/status`, `_can_business`, `_managed_link`; add `GET /onboard/bot` → 303 `/account` (old links/bookmarks).
  - `POST /onboard/profile`: create the tenant if missing (`_tenant_for`), then `await tenants.save_profile(...)`; rest unchanged.
  - `tenants.save_profile(tid, ...)`: also `write_env(tid, {"TG_BOT_TOKEN": settings.platform_bot_token, "OWNER_USER_ID": str(owner), "OWNER_FIRST_NAME": ..., "HOSTED": "1", "DASHBOARD_ROOT_PATH": "/app", "DASHBOARD_LANG": "fa"})` (look up the owner via `db.get_tenant(tid)`).
  - Remove `tenants.attach_bot`; `top_up` readiness: `if t is None or not t["profile_done"]: raise ValueError("tenant not ready")`; `delete`: remove the managed-token rotation branch.
  - `GET /account` passes `bot_username=settings.platform_bot_username`; templates use it instead of `tenant.bot_username`. Connect instructions: «تنظیمات تلگرام > Chat Automation > انتخاب @{{ bot_username }}». Delete page text: tell the user to remove the bot under Chat Automation.
  - `admin.html`: drop the bot column.
  - Delete `onboard_bot.html`; remove the onboard_bot polling from `app.js` (keep the `/account/connected` poll).
  - Step counters: onboarding is now 3 steps (consent, profile, payment) + connect on the account page.
  - Leave the `bot_id/bot_username/managed` DB columns and `db.set_tenant_bot`/`BotTaken` in place only if something still uses them; otherwise delete the unused helpers (no schema change).
- [ ] **Step 4: Run** — smoke_hosting all ok, smoke_dashboard, smoke_core. Byte-grep hosting/ for literal U+200C → 0.
- [ ] **Step 5: Commit** — `feat(hosting): onboarding without a per-user bot`.

---

### Task 4: Staging deploy (controller, with the user)

- [ ] Operator has enabled Secretary Mode on `@monshi_platform_bot`; verify `getMe.can_connect_to_business == true`.
- [ ] Stop `tgs-1` and delete its old per-user tenant (account delete from the site), upload the new release, `uv sync`, `pm2 restart tg-hosting`.
- [ ] End-to-end with a second Telegram account: login → consent → profile → admin pays $1 → Chat Automation → pick @monshi_platform_bot → account shows connected → message from a third account gets a reply → owner `/pause` in the bot DM works → `/app/` dashboard works → delete account.
