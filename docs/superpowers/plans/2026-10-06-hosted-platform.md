# Hosted Platform Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A Persian website where a non-technical user logs in with Telegram, gets a bot (one tap or pasted token), fills in a profile, and we run their tg-secretary for them on a credit-capped OpenRouter sub-key.

**Architecture:** New `hosting/` package (FastAPI control plane + raw-HTTP platform bot) beside the unchanged single-tenant `secretary/` bot. Each tenant is one pm2 process of `python -m secretary` with its own working directory (`tenants/<id>/`). The tenant's existing dashboard is reused behind a reverse proxy at `/app/`, authenticated by a per-tenant shared-secret header.

**Tech Stack:** Python 3.13, uv, FastAPI, Jinja2, httpx, aiosqlite, pydantic-settings, python-dotenv, pm2, Caddy. No new Python dependencies.

**Spec:** `docs/superpowers/specs/2026-10-06-hosted-platform-design.md`

## Global Constraints

- Package name is `hosting` (never `platform`: it shadows the stdlib module). Add it to `[tool.hatch.build.targets.wheel] packages`.
- No new dependencies. OIDC `id_token` is read without signature verification because it comes straight from Telegram's token endpoint over TLS with client-secret auth (OIDC Core 3.1.3.7). Mark that line with a `ponytail:` comment.
- Hosting config file: `hosting.env` (gitignored), example `hosting.env.example` (tracked, placeholders only). The spec's "platform.env" is this file.
- Tenant directory: `<TENANTS_ROOT>/<tenant_id>/` with `.env` (mode 600 on POSIX), `prompts/personas/*.example.txt` (copied from the repo, **never** the owner's real `*.txt` personas), `prompts/contacts/`, `prompts/about_me.txt`, `secretary.db`, `logs/`. `tenants/` is gitignored.
- Tenant `.env` keys (exact): `TG_BOT_TOKEN`, `OPENROUTER_API_KEY`, `OWNER_USER_ID`, `OWNER_FIRST_NAME`, `DASHBOARD_PORT`, `DASHBOARD_PROXY_SECRET`, `DASHBOARD_ROOT_PATH=/app`, `DASHBOARD_LANG=fa`, `HOSTED=1`.
- Proxy header (exact): `X-Platform-Auth: <DASHBOARD_PROXY_SECRET>`. Compared with `secrets.compare_digest`.
- pm2 process name: `tgs-<tenant_id>`. Started as `<repo>/.venv/bin/python -m secretary` with `--interpreter none --cwd <tenant dir>`.
- Dashboard ports: 9100–9199. Hosting web app: 127.0.0.1:8700 behind Caddy.
- Tenant status values (exact): `draft`, `awaiting_credit`, `running`, `stopped`, `deleted`.
- All user-facing website text is Persian, RTL. In `.py` files write ZWNJ as `\u200c`; in templates as `&zwnj;`. Never the invisible literal.
- Tests follow the repo style: `scripts/smoke_*.py`, plain `assert`, offline, close DB in `finally`. Run with `PYTHONIOENCODING=utf-8 uv run python scripts/<file>.py`. No pytest.
- Never `sed -i`; use Edit/Write (CRLF). Never touch production (turkey_vps); deploy to a staging server only.
- Commits: the user commits only on request. Each "Checkpoint" step below means: run the listed checks, then `git add` the listed files and commit **only if the user approved per-task commits**; otherwise leave staged.

## Review Focus

1. **Double-submitted payment form** (admin double-clicks): must record one payment and raise the key limit once. Pinned in Task 6 via `client_ref` uniqueness.
2. **Activation retried after a partial failure** (OpenRouter key created, pm2 start failed): retry must reuse the existing key, never create a second one. Pinned in Task 7.
3. **User leaves mid-onboarding and returns later**: `/account` must resume at the correct step from stored state. Pinned in Task 9 (`next_step`).
4. **Same bot claimed by two users** (pasted token or managed update for a bot already owned): second user is refused; the same user re-attaching their own bot is allowed. Pinned in Task 5 and Task 7.
5. **Persian digits in admin amount** (`۵` or `۱۲.۵`): parsed as numbers, never crash; non-numbers refused with a message. Pinned in Task 9.
6. **Opening `/app/` before the bot runs** (status not `running`, or process down): redirect to `/account` or show a Persian "starting" page, never a raw 502 stack. Pinned in Task 10.

## Execution waves

- **Wave 1 (parallel, disjoint paths):** Lane A = Tasks 1–4 (`secretary/`, `scripts/smoke_dashboard.py`). Lane B = Tasks 5–7 (`hosting/` core, `scripts/smoke_hosting.py`). Lane C = Task 12 (`hosting/templates/`, `hosting/static/`).
- **Wave 2 (sequential, after wave 1):** Tasks 9, 10, 11 (routes, proxy, platform bot). They render Lane C's templates.
- **Wave 3:** Task 13 (deploy files + staging end-to-end).

---

## Lane A — bot changes (`secretary/`)

### Task 1: Hosted mode — proxy-header auth, hidden secret fields

**Files:**
- Modify: `secretary/config.py` (add fields after `dashboard_ssh_hint`)
- Modify: `secretary/dashboard/auth.py` (add `proxy_ok`)
- Modify: `secretary/dashboard/app.py:34-47` (guard)
- Modify: `secretary/dashboard/web.py` (template globals)
- Modify: `secretary/dashboard/pages.py:150-206` (`save_config`)
- Modify: `secretary/dashboard/templates/settings.html`, `secretary/dashboard/templates/base.html`
- Modify: `secretary/commands.py:1264` (`on_dashboard`)
- Test: `scripts/smoke_dashboard.py`

**Interfaces:**
- Produces: `settings.hosted: bool`, `settings.dashboard_proxy_secret: str`, `settings.dashboard_root_path: str`, `settings.dashboard_lang: str`; `auth.proxy_ok(header: str | None) -> bool`; Jinja globals `hosted`, `root`.

- [ ] **Step 1: Add settings fields**

In `secretary/config.py`, after `dashboard_ssh_hint: str = ""`:

```python
    # Hosted platform mode: the dashboard sits behind the hosting proxy, which sends
    # X-Platform-Auth; token/key/owner fields are hidden because the platform owns them.
    hosted: bool = False
    dashboard_proxy_secret: str = ""
    # URL prefix the proxy serves the dashboard under, e.g. "/app". Empty = served at /.
    dashboard_root_path: str = ""
    dashboard_lang: str = "en"
```

- [ ] **Step 2: Write the failing test**

`scripts/smoke_dashboard.py` builds the app once at import time with env from the top of the file. Hosted checks need a different settings state, so toggle `settings` attributes inside the check and restore them in `finally`. Add:

```python
async def check_hosted() -> None:
    old = (settings.hosted, settings.dashboard_proxy_secret)
    settings.hosted, settings.dashboard_proxy_secret = True, "s3cret-proxy"
    try:
        app = create_app(BOT, lambda: STOPS.append(1), env_path=ENV)
        # No header: refused, and no redirect to the login page.
        async with client(app) as c:
            r = await c.get("/settings")
            assert r.status_code == 403, r.status_code
            r = await c.get("/settings", headers={"x-platform-auth": "wrong"})
            assert r.status_code == 403
        hdr = {"x-platform-auth": "s3cret-proxy"}
        # Header works even with a non-loopback Host (the proxy may forward any Host).
        async with client(app, base="http://example.com", headers=hdr) as c:
            r = await c.get("/settings")
            assert r.status_code == 200, r.status_code
            assert "TG_BOT_TOKEN" not in r.text and "OPENROUTER_API_KEY" not in r.text
            assert "OWNER_USER_ID" not in r.text
            assert 'action="/logout"' not in r.text
            # Saving without the hidden fields works and never touches them.
            r = await c.post("/settings/config", data={
                "OPENROUTER_MODEL": "a/b", "EXTRACTOR_MODEL": "google/gemini-3.1-flash-lite",
                "WHISPER_MODEL": "openai/whisper-large-v3", "OWNER_FIRST_NAME": "Sep",
                "HISTORY_TURNS": "12",
                "TG_BOT_TOKEN": "999:evil", "OWNER_USER_ID": "666",
            })
            assert r.status_code == 303, r.status_code
            assert "err=" not in r.headers["location"], r.headers["location"]
            env_text = ENV.read_text(encoding="utf-8")
            assert "999:evil" not in env_text and "666" not in env_text
    finally:
        settings.hosted, settings.dashboard_proxy_secret = old


async def check_proxy_ok_empty_secret() -> None:
    old = settings.dashboard_proxy_secret
    settings.dashboard_proxy_secret = ""
    try:
        assert not auth.proxy_ok("")      # empty secret never authenticates
        assert not auth.proxy_ok(None)
    finally:
        settings.dashboard_proxy_secret = old
```

Call both from the file's main runner (next to the existing `check_*` calls, inside its `try`).

- [ ] **Step 3: Run test to verify it fails**

Run: `PYTHONIOENCODING=utf-8 uv run python scripts/smoke_dashboard.py`
Expected: FAIL — `AttributeError: module 'secretary.dashboard.auth' has no attribute 'proxy_ok'` (or the 403 assert).

- [ ] **Step 4: Implement**

`secretary/dashboard/auth.py`, after `origin_ok`:

```python
def proxy_ok(header: str | None) -> bool:
    """Hosted mode: the hosting proxy proves itself with the per-tenant shared secret."""
    secret = settings.dashboard_proxy_secret
    return bool(secret) and secrets.compare_digest(header or "", secret)
```

`secretary/dashboard/app.py` guard — insert at the top of `guard`, before the host check:

```python
        if settings.hosted:
            # Only the hosting proxy may talk to us; it already checked the user's
            # session and Origin, so Host/Origin/login checks don't apply here.
            if not auth.proxy_ok(request.headers.get("x-platform-auth")):
                return PlainTextResponse("forbidden", status_code=403)
            request.state.pending = await db.count_open_pending()
            return await call_next(request)
```

`secretary/dashboard/web.py`, after the filters:

```python
from ..config import settings  # noqa: E402  (top-level import is fine; place with the others)

templates.env.globals["hosted"] = settings.hosted
templates.env.globals["root"] = settings.dashboard_root_path
```

(Put the import with the other imports at the top of the file.)

`secretary/dashboard/pages.py` `save_config`: build `current` as now, then drop owner-managed keys in hosted mode, and skip the token/key block:

```python
    if settings.hosted:
        current.pop("OWNER_USER_ID")
```

and wrap the `token = ...` / `api_key = ...` / `try:` block in `if not settings.hosted:`.

`settings.html`: wrap the `TG_BOT_TOKEN`, `OPENROUTER_API_KEY` and `OWNER_USER_ID` inputs (and their labels) in `{% if not hosted %} ... {% endif %}`.

`base.html`: wrap the logout `<form>` in `{% if not hosted %} ... {% endif %}`.

`secretary/commands.py` `on_dashboard`: after the owner guard, add:

```python
    if settings.hosted:
        await update.effective_message.reply_text("Open your dashboard on the website (حساب کاربری > داشبورد).")
        return
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `PYTHONIOENCODING=utf-8 uv run python scripts/smoke_dashboard.py && uv run python scripts/smoke_core.py && uv run python -c "import secretary.__main__; print('OK')"`
Expected: all pass, `OK`.

- [ ] **Step 6: Checkpoint** — files above.

---

### Task 2: Dashboard root-path prefix

**Files:**
- Modify: `secretary/dashboard/web.py` (`back`)
- Modify: every `RedirectResponse("/...")` in `secretary/dashboard/*.py` (find with `grep -n 'RedirectResponse("/' secretary/dashboard/*.py`)
- Modify: all 25 absolute URLs in `secretary/dashboard/templates/*.html` (find with `grep -rnE '(href|action|src)="/' secretary/dashboard/templates`)
- Test: `scripts/smoke_dashboard.py`

**Interfaces:**
- Consumes: `settings.dashboard_root_path`, Jinja global `root` (Task 1).
- Produces: every dashboard URL and redirect starts with `settings.dashboard_root_path`.

- [ ] **Step 1: Write the failing test**

```python
async def check_root_path() -> None:
    import secretary.dashboard.web as web
    old = (settings.hosted, settings.dashboard_proxy_secret, settings.dashboard_root_path)
    settings.hosted, settings.dashboard_proxy_secret, settings.dashboard_root_path = True, "p", "/app"
    web.templates.env.globals["root"] = "/app"
    try:
        app = create_app(BOT, lambda: STOPS.append(1), env_path=ENV)
        async with client(app, headers={"x-platform-auth": "p"}) as c:
            r = await c.get("/")
            assert r.status_code == 200
            for bad in ('href="/"', 'href="/settings"', 'href="/static/'):
                assert bad not in r.text, bad
            assert 'href="/app/settings"' in r.text and 'href="/app/static/style.css"' in r.text
            r = await c.post("/settings/config", data={"HISTORY_TURNS": ""})
            assert r.headers["location"].startswith("/app/settings"), r.headers["location"]
    finally:
        settings.hosted, settings.dashboard_proxy_secret, settings.dashboard_root_path = old
        web.templates.env.globals["root"] = old[2]
```

Call it from the runner.

- [ ] **Step 2: Run to verify it fails**

Run: `PYTHONIOENCODING=utf-8 uv run python scripts/smoke_dashboard.py`
Expected: FAIL on `bad not in r.text` for `href="/"`.

- [ ] **Step 3: Implement**

`web.py` `back`:

```python
def back(url: str, msg: str = "", err: str = "", **params: str) -> RedirectResponse:
    """Post-redirect-get with a one-line flash message in the query string."""
    url = settings.dashboard_root_path + url
    query = urlencode({k: v for k, v in {"msg": msg, "err": err, **params}.items() if v})
    return RedirectResponse(f"{url}?{query}" if query else url, status_code=303)
```

Each direct `RedirectResponse("/x", ...)` becomes `RedirectResponse(settings.dashboard_root_path + "/x", ...)` (import `settings` where missing).

In templates, every `href="/`, `action="/`, `src="/` becomes `href="{{ root }}/` (etc.). For the bare root link use `href="{{ root }}/"`. Do not touch `href="#..."` or full `http(s)://` URLs.

- [ ] **Step 4: Run to verify it passes**

Run: `PYTHONIOENCODING=utf-8 uv run python scripts/smoke_dashboard.py`
Expected: PASS (existing checks still pass with `root=""`).

- [ ] **Step 5: Checkpoint**

---

### Task 3: Persian dashboard strings

**Files:**
- Create: `secretary/dashboard/fa.py`
- Modify: `secretary/dashboard/web.py` (filter `t`, global `lang`)
- Modify: all templates in `secretary/dashboard/templates/` (wrap visible English text in `{{ "..."|t }}`; `base.html` gets `lang`/`dir`)
- Modify: flash strings passed to `back(...)` / `render(..., err=...)` in `secretary/dashboard/*.py` (wrap with `t(...)`)
- Test: `scripts/smoke_dashboard.py`

**Interfaces:**
- Consumes: `settings.dashboard_lang` (Task 1).
- Produces: `web.t(text: str) -> str` (returns Persian when `dashboard_lang == "fa"` and a translation exists, else the input).

- [ ] **Step 1: Write the failing test**

```python
async def check_persian() -> None:
    import secretary.dashboard.web as web
    old = (settings.hosted, settings.dashboard_proxy_secret, settings.dashboard_lang)
    settings.hosted, settings.dashboard_proxy_secret, settings.dashboard_lang = True, "p", "fa"
    web.templates.env.globals["lang"] = "fa"
    try:
        app = create_app(BOT, lambda: STOPS.append(1), env_path=ENV)
        async with client(app, headers={"x-platform-auth": "p"}) as c:
            for path in ("/", "/settings", "/contacts", "/prompts", "/drafts"):
                r = await c.get(path)
                assert r.status_code == 200, (path, r.status_code)
                assert 'dir="rtl"' in r.text and 'lang="fa"' in r.text, path
                assert "تنظیمات" in r.text, path          # nav: Settings
                for english in (">Settings<", ">Contacts<", ">Drafts<"):
                    assert english not in r.text, (path, english)
        assert web.t("never-translated-xyz") == "never-translated-xyz"
    finally:
        settings.hosted, settings.dashboard_proxy_secret, settings.dashboard_lang = old
        web.templates.env.globals["lang"] = old[2]
```

- [ ] **Step 2: Run to verify it fails**

Expected: FAIL on `dir="rtl"`.

- [ ] **Step 3: Implement**

`secretary/dashboard/fa.py`:

```python
"""Persian strings for the dashboard (hosted users). Key = the English text in the template."""

FA: dict[str, str] = {
    "Status": "وضعیت",
    "Settings": "تنظیمات",
    "Contacts": "مخاطب\u200cها",
    "Prompts": "پرامپت\u200cها",
    "Drafts": "پیش\u200cنویس\u200cها",
    "Log out": "خروج",
    "Save": "ذخیره",
    "Send": "ارسال",
    "Skip": "رد کردن",
    "Restart": "راه\u200cاندازی دوباره",
    "Nothing changed.": "چیزی تغییر نکرد.",
    "Saved and applied.": "ذخیره و اعمال شد.",
    # Add one entry per remaining visible English string in the templates and flash messages.
}
```

The worker must add an entry for **every** visible English string it wraps (walk each template top to bottom). Keep the colloquial-but-clear register; use `\u200c` for ZWNJ.

`web.py`:

```python
from .fa import FA


def t(text: str) -> str:
    return FA.get(text, text) if settings.dashboard_lang == "fa" else text


templates.env.filters["t"] = t
templates.env.globals["lang"] = settings.dashboard_lang
```

`base.html` first line after doctype: `<html lang="{{ lang }}" dir="{{ 'rtl' if lang == 'fa' else 'ltr' }}">`. Wrap nav labels: `{{ "Settings"|t }}`. Do the same in every template. In `.py` flash calls: `back("/settings", msg=t("Saved and applied."))` (import `t` from `.web`). Messages with f-string values: translate the fixed part only, e.g. `err=t("Unknown model id:") + f" {changes[key]}"`.

`static/style.css`: add `[dir="rtl"] nav, [dir="rtl"] main { text-align: right; }` and use logical properties where the file uses `margin-left/right` (swap to `margin-inline-start/end`).

- [ ] **Step 4: Run to verify it passes**

Run: `PYTHONIOENCODING=utf-8 uv run python scripts/smoke_dashboard.py`
Expected: PASS, and existing English checks still pass (default `dashboard_lang="en"`).

- [ ] **Step 5: Checkpoint**

---

### Task 4: Credit-exhausted (HTTP 402) handling

**Files:**
- Modify: `secretary/handlers.py:477-490` (the `except` around `llm.generate_reply`)
- Test: `scripts/smoke_dashboard.py` (it already opens a temp DB)

**Interfaces:**
- Produces: `handlers._credit_notice_due() -> Awaitable[bool]` (True at most once per hour; state key `credit_notice_at`).

- [ ] **Step 1: Write the failing test**

```python
async def check_credit_notice() -> None:
    from secretary import handlers
    await db.set_state("credit_notice_at", "0")
    assert await handlers._credit_notice_due() is True
    assert await handlers._credit_notice_due() is False      # within the hour
    await db.set_state("credit_notice_at", str(int(time.time()) - 3601))
    assert await handlers._credit_notice_due() is True
```

- [ ] **Step 2: Run to verify it fails**

Expected: FAIL — `AttributeError: ... '_credit_notice_due'`.

- [ ] **Step 3: Implement**

In `handlers.py`, near `_notify_owner`:

```python
async def _credit_notice_due() -> bool:
    """OpenRouter 402 = the hosted credit is used up. Tell the owner at most once an hour."""
    now = int(time.time())
    if now - int(await db.get_state("credit_notice_at") or 0) < 3600:
        return False
    await db.set_state("credit_notice_at", str(now))
    return True
```

In the `except Exception as e:` after `log.exception("LLM call failed")`:

```python
        if getattr(e, "status_code", None) == 402:
            if await _credit_notice_due():
                await _notify_owner(
                    ctx, owner_chat_id,
                    "Credit used up: no replies until you top up.\n"
                    "اعتبار تمام شده؛ تا شارژ دوباره پاسخی ارسال نمی\u200cشود.",
                )
            return
```

(`openai.APIStatusError` carries `status_code`; `getattr` keeps other exceptions on the old path. Import `time` if missing.)

- [ ] **Step 4: Run to verify it passes**

Run: `PYTHONIOENCODING=utf-8 uv run python scripts/smoke_dashboard.py && uv run python scripts/smoke_core.py`
Expected: PASS.

- [ ] **Step 5: Checkpoint**

---

## Lane B — hosting core (`hosting/`)

Lane B owns: `hosting/__init__.py`, `hosting/config.py`, `hosting/db.py`, `hosting/openrouter.py`, `hosting/tg.py`, `hosting/oidc.py`, `hosting/pm2.py`, `hosting/tenants.py`, `scripts/smoke_hosting.py`, `hosting.env.example`, `.gitignore`, `pyproject.toml` (packages line only).

`scripts/smoke_hosting.py` skeleton (created in Task 5, extended by each later task):

```python
"""Offline checks for the hosting control plane. No Telegram, OpenRouter or pm2.

    PYTHONIOENCODING=utf-8 uv run python scripts/smoke_hosting.py
"""
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

_tmp = Path(tempfile.mkdtemp())
os.environ.update(
    PLATFORM_BOT_TOKEN="1:platform", PLATFORM_BOT_USERNAME="plat_bot",
    OIDC_CLIENT_ID="cid", OIDC_CLIENT_SECRET="csecret",
    OPENROUTER_MGMT_KEY="sk-or-mgmt", PUBLIC_URL="https://host.test", ADMIN_TG_ID="1",
    DB_PATH=str(_tmp / "hosting.db"), TENANTS_ROOT=str(_tmp / "tenants"),
)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from hosting import db  # noqa: E402
from hosting.config import settings  # noqa: E402

CHECKS = []


def check(fn):
    CHECKS.append(fn)
    return fn


async def main() -> None:
    await db.init(settings.db_path)
    try:
        for fn in CHECKS:
            await fn()
            print("ok", fn.__name__)
    finally:
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
```

Each later task adds `@check async def check_x(): ...` **above** the `main` definition.

### Task 5: Hosting config + DB

**Files:**
- Create: `hosting/__init__.py` (empty), `hosting/config.py`, `hosting/db.py`, `hosting.env.example`, `scripts/smoke_hosting.py`
- Modify: `.gitignore` (add `tenants/`, `hosting.env`), `pyproject.toml` (`packages = ["secretary", "hosting"]`)

**Interfaces:**
- Produces (`hosting.db`): `init(path)`, `close()`, `upsert_user(tg_id, first_name, username)`, `get_user(tg_id) -> dict|None`, `add_consent(tg_id, version)`, `has_consent(tg_id, version) -> bool`, `create_session(tg_id) -> str`, `session_user(token) -> int|None`, `end_session(token)`, `put_oauth_state(state, verifier)`, `pop_oauth_state(state) -> str|None`, `create_tenant(owner_tg_id) -> int`, `get_tenant(tid) -> dict|None`, `get_tenant_by_owner(tg_id) -> dict|None`, `get_tenant_by_bot(bot_id) -> dict|None`, `list_tenants() -> list[dict]`, `update_tenant(tid, **fields)`, `set_tenant_bot(tid, bot_id, bot_username, managed) ` (raises `BotTaken`), `alloc_port(tid) -> int`, `add_payment(tenant_id, amount_usd, paid_text, note, admin_tg_id, client_ref) -> bool`; exception `BotTaken`.
- Produces (`hosting.config`): `settings` with fields listed below.

- [ ] **Step 1: Config**

`hosting/config.py`:

```python
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class HostingSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file="hosting.env", env_file_encoding="utf-8", extra="ignore")

    platform_bot_token: str
    platform_bot_username: str
    oidc_client_id: str
    oidc_client_secret: str
    openrouter_mgmt_key: str
    public_url: str  # https://example.com, no trailing slash
    admin_tg_id: int
    db_path: Path = Path("./hosting.db")
    tenants_root: Path = Path("./tenants")
    repo_root: Path = Path(__file__).resolve().parent.parent
    listen_port: int = 8700
    port_range_start: int = 9100
    port_range_end: int = 9199
    trial_credit_usd: float = 0.0
    consent_version: int = 1
    payment_instructions: str = ""  # shown on the awaiting-credit page (card number, wallet)


settings = HostingSettings()  # type: ignore[call-arg]
```

`hosting.env.example` lists every field above with placeholder values and a one-line comment each.

- [ ] **Step 2: Write the failing test** (in `scripts/smoke_hosting.py`, created from the skeleton)

```python
@check
async def check_db() -> None:
    await db.upsert_user(10, "Ali", "ali")
    await db.upsert_user(10, "Ali2", "ali")              # upsert, not duplicate
    assert (await db.get_user(10))["first_name"] == "Ali2"
    assert not await db.has_consent(10, 1)
    await db.add_consent(10, 1)
    assert await db.has_consent(10, 1) and not await db.has_consent(10, 2)

    s = await db.create_session(10)
    assert await db.session_user(s) == 10
    await db.end_session(s)
    assert await db.session_user(s) is None

    await db.put_oauth_state("st", "ver")
    assert await db.pop_oauth_state("st") == "ver"
    assert await db.pop_oauth_state("st") is None         # one-time

    tid = await db.create_tenant(10)
    assert (await db.get_tenant(tid))["status"] == "draft"
    assert (await db.get_tenant_by_owner(10))["id"] == tid

    await db.set_tenant_bot(tid, 555, "ali_bot", managed=False)
    await db.set_tenant_bot(tid, 555, "ali_bot", managed=False)   # same owner re-attach: fine
    await db.upsert_user(11, "Reza", "reza")
    other = await db.create_tenant(11)
    try:
        await db.set_tenant_bot(other, 555, "ali_bot", managed=False)
        raise AssertionError("bot reuse across tenants must fail")
    except db.BotTaken:
        pass

    p1 = await db.alloc_port(tid)
    assert p1 == await db.alloc_port(tid)                 # stable per tenant
    p2 = await db.alloc_port(other)
    assert p1 != p2 and settings.port_range_start <= p2 <= settings.port_range_end

    assert await db.add_payment(tid, 5.0, "500k toman", "", 1, "ref-1") is True
    assert await db.add_payment(tid, 5.0, "500k toman", "", 1, "ref-1") is False   # double submit
```

- [ ] **Step 3: Run to verify it fails**

Run: `PYTHONIOENCODING=utf-8 uv run python scripts/smoke_hosting.py`
Expected: FAIL — `ModuleNotFoundError: No module named 'hosting.db'`.

- [ ] **Step 4: Implement `hosting/db.py`**

Follow `secretary/db.py`'s pattern (one module-level aiosqlite connection, WAL).

```python
"""Hosting control-plane storage: users, sessions, tenants, payments. One shared connection."""
from __future__ import annotations

import hashlib
import secrets
import sqlite3
import time
from pathlib import Path
from typing import Any

import aiosqlite

from .config import settings

SESSION_TTL = 30 * 86400
OAUTH_TTL = 600

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    tg_id INTEGER PRIMARY KEY, first_name TEXT, username TEXT, created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS consents (
    id INTEGER PRIMARY KEY, tg_id INTEGER NOT NULL, version INTEGER NOT NULL, accepted_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY, tg_id INTEGER NOT NULL, expires_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS oauth_states (
    state TEXT PRIMARY KEY, verifier TEXT NOT NULL, expires_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS tenants (
    id INTEGER PRIMARY KEY,
    owner_tg_id INTEGER NOT NULL UNIQUE,
    bot_id INTEGER UNIQUE,
    bot_username TEXT,
    managed INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'draft',
    profile_done INTEGER NOT NULL DEFAULT 0,
    dashboard_port INTEGER UNIQUE,
    proxy_secret TEXT,
    or_key_hash TEXT,
    warned_at_limit REAL,
    last_error TEXT,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS payments (
    id INTEGER PRIMARY KEY, tenant_id INTEGER NOT NULL, amount_usd REAL NOT NULL,
    paid_text TEXT, note TEXT, admin_tg_id INTEGER NOT NULL,
    client_ref TEXT NOT NULL UNIQUE, created_at INTEGER NOT NULL
);
"""

UPDATABLE = {"bot_username", "status", "profile_done", "proxy_secret", "or_key_hash",
             "warned_at_limit", "last_error", "managed"}

_conn: aiosqlite.Connection | None = None


class BotTaken(Exception):
    """The bot is already attached to another tenant."""


def _db() -> aiosqlite.Connection:
    if _conn is None:
        raise RuntimeError("hosting.db.init() not called")
    return _conn


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def init(path: Path) -> None:
    global _conn
    _conn = await aiosqlite.connect(path)
    _conn.row_factory = aiosqlite.Row
    await _conn.execute("PRAGMA journal_mode=WAL")
    await _conn.executescript(SCHEMA)
    await _conn.commit()


async def close() -> None:
    global _conn
    if _conn is not None:
        await _conn.close()
        _conn = None


async def _one(sql: str, *args: Any) -> dict[str, Any] | None:
    async with _db().execute(sql, args) as cur:
        row = await cur.fetchone()
    return dict(row) if row else None


async def _write(sql: str, *args: Any) -> int | None:
    cur = await _db().execute(sql, args)
    await _db().commit()
    return cur.lastrowid


async def upsert_user(tg_id: int, first_name: str, username: str | None) -> None:
    await _write(
        "INSERT INTO users (tg_id, first_name, username, created_at) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(tg_id) DO UPDATE SET first_name=excluded.first_name, username=excluded.username",
        tg_id, first_name, username, int(time.time()),
    )


async def get_user(tg_id: int) -> dict[str, Any] | None:
    return await _one("SELECT * FROM users WHERE tg_id=?", tg_id)


async def add_consent(tg_id: int, version: int) -> None:
    await _write("INSERT INTO consents (tg_id, version, accepted_at) VALUES (?, ?, ?)",
                 tg_id, version, int(time.time()))


async def has_consent(tg_id: int, version: int) -> bool:
    return await _one("SELECT 1 FROM consents WHERE tg_id=? AND version=?", tg_id, version) is not None


async def create_session(tg_id: int) -> str:
    token = secrets.token_urlsafe(32)
    await _write("INSERT INTO sessions VALUES (?, ?, ?)", _hash(token), tg_id, int(time.time()) + SESSION_TTL)
    return token


async def session_user(token: str | None) -> int | None:
    if not token:
        return None
    row = await _one("SELECT tg_id FROM sessions WHERE token_hash=? AND expires_at>?", _hash(token), int(time.time()))
    return row["tg_id"] if row else None


async def end_session(token: str | None) -> None:
    if token:
        await _write("DELETE FROM sessions WHERE token_hash=?", _hash(token))


async def put_oauth_state(state: str, verifier: str) -> None:
    now = int(time.time())
    await _write("DELETE FROM oauth_states WHERE expires_at<?", now)
    await _write("INSERT INTO oauth_states VALUES (?, ?, ?)", state, verifier, now + OAUTH_TTL)


async def pop_oauth_state(state: str) -> str | None:
    row = await _one("SELECT verifier FROM oauth_states WHERE state=? AND expires_at>?", state, int(time.time()))
    await _write("DELETE FROM oauth_states WHERE state=?", state)
    return row["verifier"] if row else None


async def create_tenant(owner_tg_id: int) -> int:
    tid = await _write("INSERT INTO tenants (owner_tg_id, created_at) VALUES (?, ?)", owner_tg_id, int(time.time()))
    if tid is None:
        raise RuntimeError("tenant insert returned no rowid")
    return tid


async def get_tenant(tid: int) -> dict[str, Any] | None:
    return await _one("SELECT * FROM tenants WHERE id=?", tid)


async def get_tenant_by_owner(tg_id: int) -> dict[str, Any] | None:
    return await _one("SELECT * FROM tenants WHERE owner_tg_id=? AND status!='deleted'", tg_id)


async def get_tenant_by_bot(bot_id: int) -> dict[str, Any] | None:
    return await _one("SELECT * FROM tenants WHERE bot_id=?", bot_id)


async def list_tenants() -> list[dict[str, Any]]:
    async with _db().execute("SELECT * FROM tenants WHERE status!='deleted' ORDER BY id") as cur:
        return [dict(r) for r in await cur.fetchall()]


async def update_tenant(tid: int, **fields: Any) -> None:
    bad = fields.keys() - UPDATABLE
    if bad:
        raise ValueError(f"not updatable: {bad}")
    sets = ", ".join(f"{k}=?" for k in fields)
    await _write(f"UPDATE tenants SET {sets} WHERE id=?", *fields.values(), tid)


async def set_tenant_bot(tid: int, bot_id: int, bot_username: str, managed: bool) -> None:
    owner = await get_tenant_by_bot(bot_id)
    if owner is not None and owner["id"] != tid:
        raise BotTaken(bot_username)
    try:
        await _write("UPDATE tenants SET bot_id=?, bot_username=?, managed=? WHERE id=?",
                     bot_id, bot_username, int(managed), tid)
    except sqlite3.IntegrityError as e:  # lost a race with another attach
        raise BotTaken(bot_username) from e


async def alloc_port(tid: int) -> int:
    t = await get_tenant(tid)
    if t and t["dashboard_port"]:
        return t["dashboard_port"]
    async with _db().execute("SELECT dashboard_port FROM tenants WHERE dashboard_port IS NOT NULL") as cur:
        used = {r[0] for r in await cur.fetchall()}
    for port in range(settings.port_range_start, settings.port_range_end + 1):
        if port not in used:
            await _write("UPDATE tenants SET dashboard_port=? WHERE id=?", port, tid)
            return port
    raise RuntimeError("no free dashboard port")


async def add_payment(tenant_id: int, amount_usd: float, paid_text: str, note: str,
                      admin_tg_id: int, client_ref: str) -> bool:
    """False when client_ref was already recorded (double-submitted form)."""
    try:
        await _write(
            "INSERT INTO payments (tenant_id, amount_usd, paid_text, note, admin_tg_id, client_ref, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            tenant_id, amount_usd, paid_text, note, admin_tg_id, client_ref, int(time.time()),
        )
    except sqlite3.IntegrityError:
        return False
    return True
```

Deleting a tenant sets `status='deleted'` and clears `bot_id` and `dashboard_port` (done in Task 7 via a raw `_write`; add `release_tenant(tid)` here):

```python
async def release_tenant(tid: int) -> None:
    await _write("UPDATE tenants SET status='deleted', bot_id=NULL, dashboard_port=NULL WHERE id=?", tid)
```

Note `owner_tg_id` is `UNIQUE`, so a user who deleted their tenant cannot create a new one. Make it re-creatable: change the column to `owner_tg_id INTEGER NOT NULL` (no UNIQUE) and add `CREATE UNIQUE INDEX IF NOT EXISTS one_live_tenant ON tenants(owner_tg_id) WHERE status!='deleted';`. Add to the test: after `release_tenant(other)`, `create_tenant(11)` succeeds.

- [ ] **Step 5: Run to verify it passes**

Run: `PYTHONIOENCODING=utf-8 uv run python scripts/smoke_hosting.py`
Expected: `ok check_db`.

- [ ] **Step 6: Checkpoint**

---

### Task 6: OpenRouter key client + Telegram client + OIDC

**Files:**
- Create: `hosting/openrouter.py`, `hosting/tg.py`, `hosting/oidc.py`
- Test: `scripts/smoke_hosting.py`

**Interfaces:**
- Produces:
  - `openrouter.TRANSPORT: httpx.AsyncBaseTransport | None` (tests set it); `openrouter.OpenRouterError`; `await openrouter.create_key(name: str, limit: float) -> tuple[str, str]` (key, hash); `await openrouter.get_key(hash) -> dict` (`limit`, `usage`, `limit_remaining`, `disabled`); `await openrouter.set_limit(hash, limit: float)`; `await openrouter.disable_key(hash)`.
  - `tg.TRANSPORT`; `tg.TelegramError`; `await tg.call(token: str, method: str, **params) -> Any`.
  - `oidc.new_pkce() -> tuple[str, str]` (verifier, challenge); `oidc.auth_url(state: str, challenge: str) -> str`; `await oidc.exchange(code: str, verifier: str) -> dict` (claims); `oidc.claims_from_id_token(id_token: str, now: float) -> dict` (raises `ValueError`); `oidc.REDIRECT_PATH = "/auth/callback"`.

- [ ] **Step 1: Write the failing tests**

```python
import base64  # noqa: E402  (put with the imports at top)

from hosting import oidc, openrouter, tg  # noqa: E402


def _jwt(claims: dict) -> str:
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()
    return f"{enc({'alg': 'RS256'})}.{enc(claims)}.sig"


@check
async def check_openrouter() -> None:
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append((req.method, req.url.path, json.loads(req.content or b"{}")))
        assert req.headers["authorization"] == "Bearer sk-or-mgmt"
        if req.method == "POST":
            return httpx.Response(200, json={"key": "sk-or-v1-tenant", "data": {"hash": "h1"}})
        if req.method == "GET":
            return httpx.Response(200, json={"data": {"limit": 5, "usage": 1, "limit_remaining": 4, "disabled": False}})
        return httpx.Response(200, json={"data": {}})

    openrouter.TRANSPORT = httpx.MockTransport(handler)
    assert await openrouter.create_key("tgs-1", 5.0) == ("sk-or-v1-tenant", "h1")
    assert seen[-1] == ("POST", "/api/v1/keys", {"name": "tgs-1", "limit": 5.0, "limit_reset": None})
    assert (await openrouter.get_key("h1"))["limit_remaining"] == 4
    await openrouter.set_limit("h1", 10.0)
    assert seen[-1] == ("PATCH", "/api/v1/keys/h1", {"limit": 10.0})
    await openrouter.disable_key("h1")
    assert seen[-1] == ("PATCH", "/api/v1/keys/h1", {"disabled": True})

    openrouter.TRANSPORT = httpx.MockTransport(lambda r: httpx.Response(401, json={"error": {"message": "bad"}}))
    try:
        await openrouter.get_key("h1")
        raise AssertionError("401 must raise")
    except openrouter.OpenRouterError:
        pass


@check
async def check_tg() -> None:
    tg.TRANSPORT = httpx.MockTransport(lambda r: httpx.Response(
        200, json={"ok": True, "result": {"id": 555, "username": "ali_bot"}}))
    assert (await tg.call("1:x", "getMe"))["id"] == 555
    tg.TRANSPORT = httpx.MockTransport(lambda r: httpx.Response(
        401, json={"ok": False, "description": "Unauthorized"}))
    try:
        await tg.call("1:x", "getMe")
        raise AssertionError("must raise")
    except tg.TelegramError as e:
        assert "Unauthorized" in str(e)


@check
async def check_oidc() -> None:
    verifier, challenge = oidc.new_pkce()
    assert 43 <= len(verifier) <= 128 and "=" not in challenge
    url = oidc.auth_url("st8", challenge)
    assert url.startswith("https://oauth.telegram.org/auth?")
    for part in ("client_id=cid", "state=st8", "code_challenge_method=S256", "response_type=code",
                 "redirect_uri=https%3A%2F%2Fhost.test%2Fauth%2Fcallback"):
        assert part in url, part
    good = {"iss": "https://oauth.telegram.org", "aud": "cid", "exp": 2000, "id": 42, "name": "Ali"}
    assert oidc.claims_from_id_token(_jwt(good), now=1000)["id"] == 42
    for bad in ({**good, "iss": "https://evil"}, {**good, "aud": "other"}, {**good, "exp": 999}):
        try:
            oidc.claims_from_id_token(_jwt(bad), now=1000)
            raise AssertionError(f"accepted {bad}")
        except ValueError:
            pass
```

- [ ] **Step 2: Run to verify it fails**

Expected: FAIL — `ImportError: cannot import name 'oidc'`.

- [ ] **Step 3: Implement**

`hosting/openrouter.py`:

```python
"""OpenRouter management API: one credit-capped key per tenant."""
from __future__ import annotations

from typing import Any

import httpx

from .config import settings

BASE = "https://openrouter.ai/api/v1"
TRANSPORT: httpx.AsyncBaseTransport | None = None  # tests swap in a MockTransport


class OpenRouterError(Exception):
    pass


async def _req(method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    async with httpx.AsyncClient(base_url=BASE, timeout=20, transport=TRANSPORT,
                                 headers={"Authorization": f"Bearer {settings.openrouter_mgmt_key}"}) as c:
        r = await c.request(method, path, json=body)
    if r.status_code >= 400:
        raise OpenRouterError(f"{method} {path}: HTTP {r.status_code} {r.text[:200]}")
    return r.json()


async def create_key(name: str, limit: float) -> tuple[str, str]:
    """Returns (key, hash). The key is shown only once — write it to the tenant .env at once."""
    d = await _req("POST", "/keys", {"name": name, "limit": limit, "limit_reset": None})
    return d["key"], d["data"]["hash"]


async def get_key(key_hash: str) -> dict[str, Any]:
    return (await _req("GET", f"/keys/{key_hash}"))["data"]


async def set_limit(key_hash: str, limit: float) -> None:
    await _req("PATCH", f"/keys/{key_hash}", {"limit": limit})


async def disable_key(key_hash: str) -> None:
    await _req("PATCH", f"/keys/{key_hash}", {"disabled": True})
```

`hosting/tg.py`:

```python
"""Minimal Bot API caller for the platform bot and for checking tenant tokens."""
from __future__ import annotations

from typing import Any

import httpx

TRANSPORT: httpx.AsyncBaseTransport | None = None


class TelegramError(Exception):
    pass


async def call(token: str, method: str, **params: Any) -> Any:
    timeout = params.get("timeout", 0) + 15
    async with httpx.AsyncClient(timeout=timeout, transport=TRANSPORT) as c:
        r = await c.post(f"https://api.telegram.org/bot{token}/{method}", json=params)
    try:
        data = r.json()
    except ValueError:
        raise TelegramError(f"{method}: HTTP {r.status_code}") from None
    if not data.get("ok"):
        raise TelegramError(f"{method}: {data.get('description', r.status_code)}")
    return data["result"]
```

(Never log `r.url` — it contains the token.)

`hosting/oidc.py`:

```python
"""Log in with Telegram (OIDC, Authorization Code + PKCE)."""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
from typing import Any
from urllib.parse import urlencode

import httpx

from .config import settings

AUTH_URL = "https://oauth.telegram.org/auth"
TOKEN_URL = "https://oauth.telegram.org/token"
ISSUER = "https://oauth.telegram.org"
REDIRECT_PATH = "/auth/callback"
TRANSPORT: httpx.AsyncBaseTransport | None = None


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def new_pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    return verifier, _b64url(hashlib.sha256(verifier.encode()).digest())


def redirect_uri() -> str:
    return settings.public_url + REDIRECT_PATH


def auth_url(state: str, challenge: str) -> str:
    return AUTH_URL + "?" + urlencode({
        "client_id": settings.oidc_client_id, "redirect_uri": redirect_uri(),
        "response_type": "code", "scope": "openid profile telegram:bot_access",
        "state": state, "code_challenge": challenge, "code_challenge_method": "S256",
    })


def claims_from_id_token(id_token: str, now: float) -> dict[str, Any]:
    # ponytail: no signature check — the token comes straight from Telegram's token
    # endpoint over TLS with our client secret (OIDC Core 3.1.3.7). Verify via JWKS if
    # id_tokens ever arrive through the browser.
    try:
        payload = id_token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError) as e:
        raise ValueError("malformed id_token") from e
    aud = claims.get("aud")
    if claims.get("iss") != ISSUER:
        raise ValueError("wrong issuer")
    if settings.oidc_client_id not in (aud if isinstance(aud, list) else [aud]):
        raise ValueError("wrong audience")
    if float(claims.get("exp", 0)) <= now:
        raise ValueError("expired")
    if "id" not in claims:
        raise ValueError("no telegram id")
    return claims


async def exchange(code: str, verifier: str) -> dict[str, Any]:
    import time
    async with httpx.AsyncClient(timeout=20, transport=TRANSPORT) as c:
        r = await c.post(TOKEN_URL, auth=(settings.oidc_client_id, settings.oidc_client_secret), data={
            "grant_type": "authorization_code", "code": code,
            "redirect_uri": redirect_uri(), "code_verifier": verifier,
        })
    if r.status_code != 200:
        raise ValueError(f"token endpoint HTTP {r.status_code}")
    return claims_from_id_token(r.json()["id_token"], time.time())
```

(Move `import time` to the top of the file.)

- [ ] **Step 4: Run to verify it passes**

Run: `PYTHONIOENCODING=utf-8 uv run python scripts/smoke_hosting.py`
Expected: `ok` for `check_db`, `check_openrouter`, `check_tg`, `check_oidc`.

- [ ] **Step 5: Checkpoint**

---

### Task 7: Tenant provisioning + pm2 wrapper

**Files:**
- Create: `hosting/pm2.py`, `hosting/tenants.py`
- Test: `scripts/smoke_hosting.py`

**Interfaces:**
- Consumes: `db.*` (Task 5), `openrouter.*`, `tg.call` (Task 6).
- Produces:
  - `pm2.RUN` (swappable `subprocess.run`), `pm2.Pm2Error`, `pm2.name(tid) -> str`, `pm2.start(tid, cwd: Path)`, `pm2.restart(tid)`, `pm2.stop(tid)`, `pm2.delete(tid)`, `pm2.status() -> dict[str, dict]` (name → `{"status", "restarts"}`).
  - `tenants.tenant_dir(tid) -> Path`, `tenants.write_env(tid, values: dict[str, str])`, `await tenants.attach_bot(tenant: dict, token: str, managed: bool) -> dict` (getMe result; raises `db.BotTaken`, `tg.TelegramError`), `tenants.save_profile(tid, first_name: str, about: str, style: str, never: str)`, `await tenants.activate(tid, credit_usd: float)`, `await tenants.top_up(tid, amount_usd, paid_text, note, admin_tg_id, client_ref) -> bool`, `await tenants.delete(tid)`, `await tenants.is_connected(tid) -> bool`.

- [ ] **Step 1: Write the failing tests**

```python
import stat  # noqa: E402

from hosting import pm2, tenants  # noqa: E402

PM2_CALLS: list[list[str]] = []


def fake_run(args, **kw):
    PM2_CALLS.append(args[1:])
    import subprocess
    out = "[]" if args[1] == "jlist" else ""
    return subprocess.CompletedProcess(args, 0, stdout=out, stderr="")


@check
async def check_provisioning() -> None:
    pm2.RUN = fake_run
    or_calls = []

    def or_handler(req: httpx.Request) -> httpx.Response:
        or_calls.append((req.method, json.loads(req.content or b"{}")))
        if req.method == "POST":
            return httpx.Response(200, json={"key": "sk-or-v1-t", "data": {"hash": "hk"}})
        if req.method == "GET":
            return httpx.Response(200, json={"data": {"limit": 3.0, "usage": 0, "limit_remaining": 3.0}})
        return httpx.Response(200, json={"data": {}})

    openrouter.TRANSPORT = httpx.MockTransport(or_handler)
    tg.TRANSPORT = httpx.MockTransport(lambda r: httpx.Response(200, json={
        "ok": True, "result": {"id": 777, "username": "sara_bot", "can_connect_to_business": True}}))

    await db.upsert_user(20, "Sara", "sara")
    tid = await db.create_tenant(20)
    me = await tenants.attach_bot(await db.get_tenant(tid), "777:tok", managed=False)
    assert me["username"] == "sara_bot"
    env = (tenants.tenant_dir(tid) / ".env").read_text(encoding="utf-8")
    assert "777:tok" in env and "OWNER_USER_ID='20'" in env and "HOSTED='1'" in env
    if os.name == "posix":
        assert stat.S_IMODE((tenants.tenant_dir(tid) / ".env").stat().st_mode) == 0o600
    personas = {p.name for p in (tenants.tenant_dir(tid) / "prompts" / "personas").iterdir()}
    assert personas and all(n.endswith(".example.txt") for n in personas), personas  # never real personas

    tenants.save_profile(tid, "Sara", "I study law", "short, lowercase", "never promise money")
    about = (tenants.tenant_dir(tid) / "prompts" / "about_me.txt").read_text(encoding="utf-8")
    assert "I study law" in about and "never promise money" in about
    assert (await db.get_tenant(tid))["profile_done"] == 1

    # Activation: pm2 fails the first time -> key kept, retry reuses it.
    def failing_run(args, **kw):
        import subprocess
        PM2_CALLS.append(args[1:])
        return subprocess.CompletedProcess(args, 1, stdout="", stderr="boom")
    pm2.RUN = failing_run
    try:
        await tenants.activate(tid, 3.0)
        raise AssertionError("pm2 failure must raise")
    except pm2.Pm2Error:
        pass
    t = await db.get_tenant(tid)
    assert t["or_key_hash"] == "hk" and t["status"] != "running"
    pm2.RUN = fake_run
    await tenants.activate(tid, 3.0)
    assert [m for m, _ in or_calls].count("POST") == 1, or_calls      # one key only
    assert (await db.get_tenant(tid))["status"] == "running"
    start = next(c for c in PM2_CALLS if c[0] == "start")
    assert "tgs-%d" % tid in start and "--cwd" in start and "-m" in start and "secretary" in start
    env = (tenants.tenant_dir(tid) / ".env").read_text(encoding="utf-8")
    assert "sk-or-v1-t" in env and "DASHBOARD_PROXY_SECRET=" in env

    # Top-up: limit 3 + 2 = 5, double submit ignored.
    assert await tenants.top_up(tid, 2.0, "200k", "", 1, "r-9") is True
    assert await tenants.top_up(tid, 2.0, "200k", "", 1, "r-9") is False
    patches = [b for m, b in or_calls if m == "PATCH"]
    assert patches == [{"limit": 5.0}], patches

    # Another user cannot claim sara_bot.
    await db.upsert_user(21, "X", None)
    other = await db.create_tenant(21)
    try:
        await tenants.attach_bot(await db.get_tenant(other), "777:tok", managed=False)
        raise AssertionError("must refuse")
    except db.BotTaken:
        pass

    # Connected check reads the tenant's own secretary.db.
    assert await tenants.is_connected(tid) is False
    import sqlite3
    con = sqlite3.connect(tenants.tenant_dir(tid) / "secretary.db")
    con.execute("CREATE TABLE connections (conn_id TEXT, owner_user_id INTEGER, is_enabled INTEGER)")
    con.execute("INSERT INTO connections VALUES ('c', 20, 1)")
    con.commit()
    con.close()
    assert await tenants.is_connected(tid) is True

    await tenants.delete(tid)
    assert not tenants.tenant_dir(tid).exists()
    assert ["delete", "tgs-%d" % tid] in PM2_CALLS
    assert {"disabled": True} in [b for m, b in or_calls if m == "PATCH"]
    assert await db.get_tenant_by_owner(20) is None
```

- [ ] **Step 2: Run to verify it fails**

Expected: FAIL — `ImportError: cannot import name 'pm2'`.

- [ ] **Step 3: Implement `hosting/pm2.py`**

```python
"""The only place that shells out to pm2. One process per tenant: tgs-<id>."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from .config import settings

RUN = subprocess.run  # tests swap this


class Pm2Error(Exception):
    pass


def name(tid: int) -> str:
    return f"tgs-{tid}"


def _pm2(*args: str) -> str:
    r = RUN(["pm2", *args], capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise Pm2Error((r.stderr or r.stdout).strip()[:300])
    return r.stdout


def start(tid: int, cwd: Path) -> None:
    python = settings.repo_root / ".venv" / "bin" / "python"
    _pm2("start", str(python), "--name", name(tid), "--interpreter", "none", "--cwd", str(cwd),
         "--max-memory-restart", "300M", "--restart-delay", "5000", "--time",
         "--output", str(cwd / "logs" / "out.log"), "--error", str(cwd / "logs" / "err.log"),
         "--", "-m", "secretary")
    _pm2("save")


def restart(tid: int) -> None:
    _pm2("restart", name(tid))


def stop(tid: int) -> None:
    _pm2("stop", name(tid))
    _pm2("save")


def delete(tid: int) -> None:
    try:
        _pm2("delete", name(tid))
    except Pm2Error as e:
        if "not found" not in str(e).lower():
            raise
    _pm2("save")


def status() -> dict[str, dict]:
    procs = json.loads(_pm2("jlist") or "[]")
    return {p["name"]: {"status": p["pm2_env"]["status"], "restarts": p["pm2_env"]["restart_time"]}
            for p in procs if p["name"].startswith("tgs-")}
```

- [ ] **Step 4: Implement `hosting/tenants.py`**

```python
"""Tenant lifecycle: files on disk, OpenRouter key, pm2 process."""
from __future__ import annotations

import asyncio
import os
import secrets
import shutil
import sqlite3
from pathlib import Path

from dotenv import set_key

from . import db, openrouter, pm2, tg
from .config import settings


def tenant_dir(tid: int) -> Path:
    return settings.tenants_root / str(tid)


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


async def attach_bot(tenant: dict, token: str, managed: bool) -> dict:
    me = await tg.call(token, "getMe")
    await db.set_tenant_bot(tenant["id"], me["id"], me.get("username", ""), managed)
    user = await db.get_user(tenant["owner_tg_id"])
    write_env(tenant["id"], {
        "TG_BOT_TOKEN": token, "OWNER_USER_ID": str(tenant["owner_tg_id"]),
        "OWNER_FIRST_NAME": (user or {}).get("first_name") or "the owner",
        "HOSTED": "1", "DASHBOARD_ROOT_PATH": "/app", "DASHBOARD_LANG": "fa",
    })
    if tenant["status"] == "running":
        await asyncio.to_thread(pm2.restart, tenant["id"])
    return me


def save_profile(tid: int, first_name: str, about: str, style: str, never: str) -> None:
    parts = [about.strip()]
    if style.strip():
        parts.append(f"How I write: {style.strip()}")
    if never.strip():
        parts.append(f"Never: {never.strip()}")
    (_ensure_dir(tid) / "prompts" / "about_me.txt").write_text("\n\n".join(p for p in parts if p) + "\n", encoding="utf-8")
    write_env(tid, {"OWNER_FIRST_NAME": first_name.strip() or "the owner"})


async def activate(tid: int, credit_usd: float) -> None:
    """Idempotent: a retry after a partial failure reuses the key, port and secret."""
    t = await db.get_tenant(tid)
    if t is None:
        raise ValueError(f"no tenant {tid}")
    if not t["or_key_hash"]:
        key, key_hash = await openrouter.create_key(pm2.name(tid), credit_usd)
        write_env(tid, {"OPENROUTER_API_KEY": key})  # shown once: persist before anything else
        await db.update_tenant(tid, or_key_hash=key_hash)
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
    if not await db.add_payment(tid, amount_usd, paid_text, note, admin_tg_id, client_ref):
        return False
    t = await db.get_tenant(tid)
    if t and t["or_key_hash"]:
        info = await openrouter.get_key(t["or_key_hash"])
        await openrouter.set_limit(t["or_key_hash"], float(info.get("limit") or 0) + amount_usd)
        await db.update_tenant(tid, warned_at_limit=None)
    if t and t["status"] == "awaiting_credit":
        await activate(tid, amount_usd)
    return True


async def delete(tid: int) -> None:
    t = await db.get_tenant(tid)
    if t is None:
        return
    await asyncio.to_thread(pm2.delete, tid)
    if t["or_key_hash"]:
        await openrouter.disable_key(t["or_key_hash"])
    if t["managed"] and t["bot_id"]:
        # Kill the old token so nothing we stored can still drive the bot.
        await tg.call(settings.platform_bot_token, "replaceManagedBotToken", user_id=t["bot_id"])
    shutil.rmtree(tenant_dir(tid), ignore_errors=True)
    await db.release_tenant(tid)


def _connected(path: Path, owner: int) -> bool:
    if not path.exists():
        return False
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return con.execute("SELECT 1 FROM connections WHERE owner_user_id=? AND is_enabled=1",
                           (owner,)).fetchone() is not None
    except sqlite3.OperationalError:  # bot hasn't created its tables yet
        return False
    finally:
        con.close()


async def is_connected(tid: int) -> bool:
    t = await db.get_tenant(tid)
    return bool(t) and await asyncio.to_thread(_connected, tenant_dir(tid) / "secretary.db", t["owner_tg_id"])
```

Before writing `_connected`, confirm the real column names with `grep -n "CREATE TABLE IF NOT EXISTS connections" -A 10 secretary/db.py` and adjust the query and the test's fake table to match.

- [ ] **Step 5: Run to verify it passes**

Run: `PYTHONIOENCODING=utf-8 uv run python scripts/smoke_hosting.py`
Expected: all checks `ok`.

- [ ] **Step 6: Checkpoint**

---

## Lane C — Persian frontend

### Task 12: Hosting templates and static files

**Files (owned):** `hosting/templates/*.html`, `hosting/static/style.css`, `hosting/static/app.js`, `hosting/static/fonts/Vazirmatn-*.woff2`, `hosting/static/fonts/OFL.txt`

**Interfaces (the contract Task 9 renders against — exact names):**

All templates extend `base.html`. Every template receives `request`, `user` (dict or None: `tg_id`, `first_name`), `msg`, `err` (strings, may be empty). Forms POST to the listed paths; every form is a normal HTML form (no JS required to submit).

| Template | Extra context | Forms / behavior |
|---|---|---|
| `index.html` | — | Landing: what the product does (Persian, plain), how it works in 4 steps, "ورود با تلگرام" button → `GET /login` |
| `consent.html` | `version: int` | Shows the consent text below; checkbox + submit → `POST /onboard/consent` (field `accept=1`) |
| `onboard_bot.html` | `managed_link: str`, `bot_username: str` (empty if none), `needs_secretary: bool` | (1) Big button to `managed_link` ("ساخت ربات با یک لمس"); (2) collapsible "توکن دارم" form → `POST /onboard/bot/token` (field `token`); (3) JS polls `GET /onboard/bot/status` every 3 s → `{"attached": bool, "username": str, "needs_secretary": bool}`; when attached, reload. If `needs_secretary`: show steps "در @BotFather ربات را باز کنید > Bot Settings > Secretary Mode را روشن کنید", then a "بررسی دوباره" link to `/onboard/bot` |
| `onboard_profile.html` | `first_name: str` | `POST /onboard/profile` fields `first_name`, `about`, `style`, `never` (textareas, `dir="auto"`); short hints under each |
| `account.html` | `tenant: dict` (`status`, `bot_username`), `credit: dict|None` (`limit`, `usage`, `limit_remaining`), `payment_instructions: str`, `connected: bool` | Sections by `tenant.status`: `awaiting_credit` → payment instructions + "after payment, credit is added by the admin"; `running` → credit bar, connection status (JS polls `GET /account/connected` → `{"connected": bool}` every 5 s until true) with steps "تنظیمات تلگرام > Chat Automation > انتخاب @bot", link "داشبورد" → `/app/`; delete form → `POST /account/delete` with text field `confirm` (user must type `حذف`) |
| `admin.html` | `rows: list[dict]` (`id`, `owner_tg_id`, `first_name`, `bot_username`, `status`, `limit`, `usage`, `pm2_status`, `restarts`, `last_error`), `client_ref: str` | Table; per row: payment form → `POST /admin/payment` (`tenant_id`, `amount_usd`, `paid_text`, `note`, hidden `client_ref`), buttons `POST /admin/tenant/{id}/restart`, `POST /admin/tenant/{id}/stop` |
| `error.html` | `title: str`, `body: str` | Friendly Persian error, link back to `/account` |

`base.html`: `<html lang="fa" dir="rtl">`, `<link rel="stylesheet" href="/hstatic/style.css">`, `<script src="/hstatic/app.js" defer></script>`; header with product name and, when `user`, links "حساب کاربری" (`/account`) and a logout form (`POST /logout`); flash `msg`/`err` paragraphs.

Static is served at `/hstatic/` (not `/static/`, which belongs to the proxied dashboard path space).

Consent text (use verbatim in `consent.html`, version 1):

> **پیش از شروع، این‌ها را بدانید:**
> - این سرویس با ربات خودتان به پیام‌های خصوصی شما در تلگرام، از طرف شما پاسخ می‌دهد. شما در تنظیمات تلگرام انتخاب می‌کنید به کدام چت‌ها دسترسی داشته باشد.
> - برای نوشتن پاسخ، متن پیام‌ها و پیام‌های صوتی (پس از تبدیل به متن) به مدل‌های هوش مصنوعی از طریق سرویس OpenRouter فرستاده می‌شود.
> - تاریخچه‌ی گفتگو و خلاصه‌ای از هر مخاطب روی سرور ما ذخیره می‌شود تا پاسخ‌ها شبیه شما باشد. این داده برای هیچ کار دیگری استفاده نمی‌شود و به کسی فروخته نمی‌شود.
> - هر زمان بخواهید با «حذف حساب» همه‌ی داده‌های شما پاک و ربات خاموش می‌شود.
> - ربات هیچ کاری را از شما پنهان نمی‌کند؛ همه‌ی پاسخ‌ها در چت‌های خودتان دیده می‌شوند.
> - اگر این شرایط تغییر کند، پیش از اعمال به شما اطلاع می‌دهیم.

(When writing it into the template, ZWNJ characters are `&zwnj;`.)

- [ ] **Step 1:** Invoke the `frontend-design` skill. Direction: calm, trustworthy, mobile-first (most users open it from Telegram on a phone), Persian typography first. Avoid: generic purple gradients, emoji-heavy UI, English text anywhere visible.
- [ ] **Step 2:** Download Vazirmatn (OFL) woff2 Regular + Bold from the GitHub release `rastikerdar/vazirmatn` into `hosting/static/fonts/`, plus its `OFL.txt`. `@font-face` with fallback `Tahoma, sans-serif`.
- [ ] **Step 3:** Write the templates and `app.js` (only the two polling loops and the token-form toggle; no framework).
- [ ] **Step 4: Verify the templates render with the contract context** — create a throwaway script in the scratchpad (not in the repo) that renders each template with Jinja2 and sample context matching the table, writes HTML files, and open them in Chrome DevTools MCP at 400 px and 1280 px width. Check: RTL layout, no English visible, forms have the exact field names above.
- [ ] **Step 5: Checkpoint**

---

## Wave 2 — routes, proxy, platform bot (after wave 1)

### Task 9: Hosting web app — login, onboarding, account, admin

**Files:**
- Create: `hosting/app.py`, `hosting/__main__.py`
- Test: `scripts/smoke_hosting.py`

**Interfaces:**
- Consumes: everything in Tasks 5–7, templates from Task 12.
- Produces: `create_app() -> FastAPI`, `next_step(user_id: int) -> Awaitable[str]` returning one of `"consent" | "bot" | "profile" | "payment" | "account"`, cookie name `COOKIE = "hs_session"`, `parse_amount(raw: str) -> float | None`.

- [ ] **Step 1: Write the failing tests**

```python
from hosting import app as happ  # noqa: E402

BASE = "https://host.test"


def web(app, cookie: str | None = None) -> httpx.AsyncClient:
    headers = {"origin": BASE}
    if cookie:
        headers["cookie"] = f"{happ.COOKIE}={cookie}"
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE, headers=headers)


@check
async def check_routes() -> None:
    app = happ.create_app()
    async with web(app) as c:
        assert (await c.get("/")).status_code == 200
        r = await c.get("/account")
        assert r.status_code == 303 and r.headers["location"] == "/login"
        r = await c.get("/login")
        assert r.headers["location"].startswith("https://oauth.telegram.org/auth?")
        state = r.headers["location"].split("state=")[1].split("&")[0]

    # Callback with a stubbed token endpoint.
    claims = {"iss": "https://oauth.telegram.org", "aud": "cid", "exp": 4e9, "id": 30, "name": "Mina",
              "preferred_username": "mina"}
    oidc.TRANSPORT = httpx.MockTransport(lambda r: httpx.Response(200, json={"id_token": _jwt(claims)}))
    async with web(app) as c:
        r = await c.get(f"/auth/callback?code=abc&state={state}")
        assert r.status_code == 303 and r.headers["location"] == "/account"
        cookie = r.cookies[happ.COOKIE]
        r = await c.get(f"/auth/callback?code=abc&state={state}")      # state is one-time
        assert r.status_code == 400

    async with web(app, cookie) as c:
        # Resume logic (Review Focus 3).
        assert await happ.next_step(30) == "consent"
        assert (await c.get("/account")).headers["location"] == "/onboard/consent"
        assert (await c.post("/onboard/bot/token", data={"token": "1:x"})).status_code == 303
        assert await db.get_tenant_by_owner(30) is None                # no tenant without consent
        await c.post("/onboard/consent", data={"accept": "1"})
        assert await happ.next_step(30) == "bot"

        tg.TRANSPORT = httpx.MockTransport(lambda r: httpx.Response(200, json={
            "ok": True, "result": {"id": 901, "username": "mina_bot", "can_connect_to_business": False}}))
        r = await c.post("/onboard/bot/token", data={"token": "901:abc"})
        assert r.status_code == 303
        st = (await c.get("/onboard/bot/status")).json()
        assert st == {"attached": True, "username": "mina_bot", "needs_secretary": True}
        assert await happ.next_step(30) == "profile"

        await c.post("/onboard/profile", data={"first_name": "Mina", "about": "x", "style": "", "never": ""})
        assert await happ.next_step(30) == "payment"                   # trial credit is 0
        assert (await db.get_tenant_by_owner(30))["status"] == "awaiting_credit"

        # Non-admin cannot reach admin.
        assert (await c.get("/admin")).status_code == 403
        # Cross-site POST refused.
        r = await c.post("/onboard/consent", data={"accept": "1"}, headers={"origin": "https://evil.test"})
        assert r.status_code == 403


@check
async def check_amounts() -> None:
    assert happ.parse_amount("5") == 5.0
    assert happ.parse_amount("۵") == 5.0
    assert happ.parse_amount("۱۲.۵") == 12.5
    assert happ.parse_amount("۱۲٫۵") == 12.5          # Persian decimal separator
    for bad in ("", "abc", "-3", "0", "1e9"):
        assert happ.parse_amount(bad) is None, bad
```

- [ ] **Step 2: Run to verify it fails**

Expected: FAIL — `ImportError: cannot import name 'app' from 'hosting'`.

- [ ] **Step 3: Implement `hosting/app.py`**

```python
"""Hosting website: Telegram login, onboarding, account, admin, and the /app/ proxy."""
from __future__ import annotations

import asyncio
import logging
import secrets
import unicodedata
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, Request
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import db, oidc, openrouter, pm2, tenants, tg
from .config import settings

log = logging.getLogger(__name__)
_HERE = Path(__file__).resolve().parent
COOKIE = "hs_session"
templates = Jinja2Templates(directory=str(_HERE / "templates"))
PUBLIC_PATHS = {"/", "/login", oidc.REDIRECT_PATH}
MAX_AMOUNT = 1000.0


def parse_amount(raw: str) -> float | None:
    """USD amount from an admin form; accepts Persian digits and the ٫ separator."""
    text = unicodedata.normalize("NFKC", raw.strip()).replace("٫", ".")
    try:
        value = float("".join(str(unicodedata.digit(ch)) if ch.isdigit() else ch for ch in text))
    except ValueError:
        return None
    return value if 0 < value <= MAX_AMOUNT else None


async def next_step(tg_id: int) -> str:
    if not await db.has_consent(tg_id, settings.consent_version):
        return "consent"
    t = await db.get_tenant_by_owner(tg_id)
    if t is None or not t["bot_id"]:
        return "bot"
    if not t["profile_done"]:
        return "profile"
    if t["status"] in ("draft", "awaiting_credit"):
        return "payment"
    return "account"


STEP_URL = {"consent": "/onboard/consent", "bot": "/onboard/bot", "profile": "/onboard/profile"}


def render(request: Request, name: str, status_code: int = 200, **ctx: Any):
    ctx.setdefault("user", getattr(request.state, "user", None))
    ctx.setdefault("msg", request.query_params.get("msg", ""))
    ctx.setdefault("err", request.query_params.get("err", ""))
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)


def go(url: str, **q: str) -> RedirectResponse:
    from urllib.parse import urlencode
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

    @app.get("/login")
    async def login():
        verifier, challenge = oidc.new_pkce()
        state = secrets.token_urlsafe(24)
        await db.put_oauth_state(state, verifier)
        return RedirectResponse(oidc.auth_url(state, challenge), status_code=303)

    @app.get(oidc.REDIRECT_PATH)
    async def callback(request: Request, code: str = "", state: str = ""):
        verifier = await db.pop_oauth_state(state) if state else None
        if not verifier or not code:
            return render(request, "error.html", 400, title="ورود ناموفق", body="لینک ورود منقضی شده؛ دوباره وارد شوید.")
        try:
            claims = await oidc.exchange(code, verifier)
        except Exception:  # noqa: BLE001 — any failure here is "login failed" to the user
            log.exception("oidc exchange failed")
            return render(request, "error.html", 400, title="ورود ناموفق", body="دوباره تلاش کنید.")
        tg_id = int(claims["id"])
        await db.upsert_user(tg_id, claims.get("name") or claims.get("given_name") or "",
                             claims.get("preferred_username"))
        resp = RedirectResponse("/account", status_code=303)
        resp.set_cookie(COOKIE, await db.create_session(tg_id), max_age=db.SESSION_TTL,
                        httponly=True, secure=True, samesite="lax", path="/")
        return resp

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
                      connected=await tenants.is_connected(t["id"]))

    @app.get("/account/connected")
    async def account_connected(request: Request):
        t = await db.get_tenant_by_owner(uid(request))
        return JSONResponse({"connected": bool(t) and await tenants.is_connected(t["id"])})

    @app.get("/onboard/consent")
    async def consent_page(request: Request):
        return render(request, "consent.html", version=settings.consent_version)

    @app.post("/onboard/consent")
    async def consent(request: Request, accept: str = Form("")):
        if accept != "1":
            return go("/onboard/consent", err="برای ادامه باید شرایط را بپذیرید.")
        await db.add_consent(uid(request), settings.consent_version)
        return RedirectResponse("/account", status_code=303)

    async def _tenant_for(request: Request) -> dict:
        t = await db.get_tenant_by_owner(uid(request))
        if t is None:
            t = await db.get_tenant(await db.create_tenant(uid(request)))
        return t

    def _managed_link(request: Request) -> str:
        suggested = f"{(request.state.user.get('username') or 'my')}_secretary_bot"[:32]
        return f"https://t.me/newbot/{settings.platform_bot_username}/{suggested}?name=Secretary"

    @app.get("/onboard/bot")
    async def bot_page(request: Request):
        if await next_step(uid(request)) == "consent":
            return RedirectResponse("/onboard/consent", status_code=303)
        t = await db.get_tenant_by_owner(uid(request))
        return render(request, "onboard_bot.html", managed_link=_managed_link(request),
                      bot_username=(t or {}).get("bot_username") or "",
                      needs_secretary=bool(t and t["bot_id"]) and not await _can_business(t))

    async def _can_business(t: dict) -> bool:
        from dotenv import dotenv_values
        token = dotenv_values(tenants.tenant_dir(t["id"]) / ".env").get("TG_BOT_TOKEN")
        if not token:
            return False
        try:
            return bool((await tg.call(token, "getMe")).get("can_connect_to_business"))
        except tg.TelegramError:
            return False

    @app.post("/onboard/bot/token")
    async def bot_token(request: Request, token: str = Form("")):
        if await next_step(uid(request)) == "consent":
            return RedirectResponse("/onboard/consent", status_code=303)
        token = token.strip()
        if ":" not in token:
            return go("/onboard/bot", err="توکن درست نیست.")
        try:
            await tenants.attach_bot(await _tenant_for(request), token, managed=False)
        except db.BotTaken:
            return go("/onboard/bot", err="این ربات قبلاً به حساب دیگری وصل شده.")
        except tg.TelegramError:
            return go("/onboard/bot", err="تلگرام این توکن را نپذیرفت.")
        return RedirectResponse("/account", status_code=303)

    @app.get("/onboard/bot/status")
    async def bot_status(request: Request):
        t = await db.get_tenant_by_owner(uid(request))
        attached = bool(t and t["bot_id"])
        return JSONResponse({"attached": attached, "username": (t or {}).get("bot_username") or "",
                             "needs_secretary": attached and not await _can_business(t)})

    @app.get("/onboard/profile")
    async def profile_page(request: Request):
        return render(request, "onboard_profile.html", first_name=request.state.user["first_name"] or "")

    @app.post("/onboard/profile")
    async def profile(request: Request, first_name: str = Form(""), about: str = Form(""),
                      style: str = Form(""), never: str = Form("")):
        t = await db.get_tenant_by_owner(uid(request))
        if t is None or not t["bot_id"]:
            return RedirectResponse("/account", status_code=303)
        await asyncio.to_thread(tenants.save_profile, t["id"], first_name, about, style, never)
        await db.update_tenant(t["id"], profile_done=1)
        if t["status"] == "draft":
            if settings.trial_credit_usd > 0:
                try:
                    await tenants.activate(t["id"], settings.trial_credit_usd)
                except Exception:  # noqa: BLE001 — admin sees last_error and retries
                    log.exception("trial activation failed")
                    await db.update_tenant(t["id"], status="awaiting_credit")
            else:
                await db.update_tenant(t["id"], status="awaiting_credit")
        return RedirectResponse("/account", status_code=303)

    @app.post("/account/delete")
    async def delete(request: Request, confirm: str = Form("")):
        if confirm.strip() != "حذف":
            return go("/account", err="برای حذف، کلمه\u200cی «حذف» را بنویسید.")
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
                         "restarts": p.get("restarts", 0)})
        return render(request, "admin.html", rows=rows, client_ref=secrets.token_urlsafe(12))

    @app.post("/admin/payment")
    async def admin_payment(request: Request, tenant_id: int = Form(...), amount_usd: str = Form(""),
                            paid_text: str = Form(""), note: str = Form(""), client_ref: str = Form("")):
        amount = parse_amount(amount_usd)
        if amount is None or not client_ref:
            return go("/admin", err="مبلغ درست نیست.")
        try:
            added = await tenants.top_up(tenant_id, amount, paid_text, note, uid(request), client_ref)
        except Exception as e:  # noqa: BLE001 — show the failure, payment row is kept for retry
            log.exception("top-up failed")
            await db.update_tenant(tenant_id, last_error=f"top-up: {e}")
            return go("/admin", err=f"خطا: {e}")
        return go("/admin", msg="ثبت شد." if added else "این پرداخت قبلاً ثبت شده بود.")

    @app.post("/admin/tenant/{tid}/restart")
    async def admin_restart(tid: int):
        t = await db.get_tenant(tid)
        if t is None or t["status"] not in ("running", "stopped"):
            return go("/admin", err="این حساب هنوز فعال نشده.")
        try:
            await asyncio.to_thread(pm2.restart, tid)  # pm2 restart also revives a stopped process
        except pm2.Pm2Error:
            await tenants.activate(tid, 0)  # process missing from pm2: start it (reuses key, port, secret)
        await db.update_tenant(tid, status="running")
        return go("/admin", msg="انجام شد.")

    @app.post("/admin/tenant/{tid}/stop")
    async def admin_stop(tid: int):
        await asyncio.to_thread(pm2.stop, tid)
        await db.update_tenant(tid, status="stopped")
        return go("/admin", msg="متوقف شد.")

    from .proxy import router as proxy_router  # Task 10
    app.include_router(proxy_router)
    return app
```

Until Task 10 lands, create `hosting/proxy.py` with just `from fastapi import APIRouter` and `router = APIRouter()` so the import works.

`hosting/__main__.py`:

```python
"""Run the hosting site: python -m hosting (behind Caddy on 127.0.0.1)."""
import asyncio
import logging

import uvicorn

from . import db
from .app import create_app
from .bot import run_platform_bot  # Task 11
from .config import settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)  # request URLs carry bot tokens


async def main() -> None:
    await db.init(settings.db_path)
    settings.tenants_root.mkdir(parents=True, exist_ok=True)
    server = uvicorn.Server(uvicorn.Config(create_app(), host="127.0.0.1", port=settings.listen_port,
                                           log_config=None, access_log=False))
    bot = asyncio.create_task(run_platform_bot())
    try:
        await server.serve()
    finally:
        bot.cancel()
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
```

Until Task 11 lands, `hosting/bot.py` contains `async def run_platform_bot() -> None: return None`.

- [ ] **Step 4: Run to verify it passes**

Run: `PYTHONIOENCODING=utf-8 uv run python scripts/smoke_hosting.py && uv run python -c "import hosting.app; print('OK')"`
Expected: all `ok`, `OK`.

- [ ] **Step 5: Checkpoint**

---

### Task 10: `/app/` reverse proxy to the tenant dashboard

**Files:**
- Modify: `hosting/proxy.py`
- Test: `scripts/smoke_hosting.py`

**Interfaces:**
- Consumes: `db.get_tenant_by_owner`, `request.state.user` (Task 9 guard), tenant `dashboard_port` and `proxy_secret` (Task 7).
- Produces: `proxy.router`, `proxy.TRANSPORT` (tests swap).

- [ ] **Step 1: Write the failing test**

```python
from hosting import proxy  # noqa: E402


@check
async def check_proxy() -> None:
    app = happ.create_app()
    await db.upsert_user(40, "P", None)
    tid = await db.create_tenant(40)
    cookie = await db.create_session(40)
    async with web(app, cookie) as c:
        r = await c.get("/app/")                                   # not running yet (Review Focus 6)
        assert r.status_code == 303 and r.headers["location"] == "/account"

    await db.update_tenant(tid, status="running", proxy_secret="sek")
    port = await db.alloc_port(tid)
    seen = []

    def upstream(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(303, headers={"location": "/app/settings?msg=ok", "set-cookie": "x=1"})

    proxy.TRANSPORT = httpx.MockTransport(upstream)
    async with web(app, cookie) as c:
        r = await c.post("/app/settings/config?a=1", data={"HISTORY_TURNS": "9"})
        assert r.status_code == 303 and r.headers["location"] == "/app/settings?msg=ok"
        assert "set-cookie" not in r.headers or "x=1" not in r.headers.get("set-cookie", "")
    req = seen[-1]
    assert str(req.url) == f"http://127.0.0.1:{port}/settings/config?a=1"
    assert req.headers["x-platform-auth"] == "sek"
    assert b"HISTORY_TURNS=9" in req.content
    assert "cookie" not in req.headers                              # platform session never leaks

    def down(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")
    proxy.TRANSPORT = httpx.MockTransport(down)
    async with web(app, cookie) as c:
        r = await c.get("/app/")
        assert r.status_code == 503 and "ربات" in r.text           # Persian "starting" page

    async with web(app) as c:                                       # no session
        assert (await c.get("/app/")).status_code == 303
```

- [ ] **Step 2: Run to verify it fails**

Expected: FAIL — 404 on `/app/`.

- [ ] **Step 3: Implement `hosting/proxy.py`**

```python
"""/app/* -> the tenant's own dashboard on 127.0.0.1:<port>, authenticated by X-Platform-Auth."""
from __future__ import annotations

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, Response

from . import db

router = APIRouter()
TRANSPORT: httpx.AsyncBaseTransport | None = None
_PASS_REQ = ("content-type", "accept")
_PASS_RESP = ("content-type", "location", "cache-control")


@router.api_route("/app/{path:path}", methods=["GET", "POST"])
async def proxy(path: str, request: Request):
    from .app import render  # late import: app imports this module
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
        return render(request, "error.html", 503, title="ربات در حال راه\u200cاندازی است",
                      body="چند ثانیه دیگر صفحه را دوباره باز کنید.")
    return Response(r.content, r.status_code, {k: v for k in _PASS_RESP if (v := r.headers.get(k))})
```

- [ ] **Step 4: Run to verify it passes**

Run: `PYTHONIOENCODING=utf-8 uv run python scripts/smoke_hosting.py`
Expected: all `ok`.

- [ ] **Step 5: Checkpoint**

---

### Task 11: Platform bot — managed bots, /start, credit warnings

**Files:**
- Modify: `hosting/bot.py`
- Test: `scripts/smoke_hosting.py`

**Interfaces:**
- Consumes: `tg.call`, `tenants.attach_bot`, `openrouter.get_key`, `db.*`.
- Produces: `run_platform_bot()`, `handle_update(u: dict)`, `check_credit_once()`.

- [ ] **Step 1: Write the failing test**

```python
from hosting import bot as pbot  # noqa: E402


@check
async def check_platform_bot() -> None:
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        method = req.url.path.rsplit("/", 1)[1]
        body = json.loads(req.content or b"{}")
        calls.append((method, body))
        if method == "getManagedBotToken":
            return httpx.Response(200, json={"ok": True, "result": "950:managed"})
        if method == "getMe":
            return httpx.Response(200, json={"ok": True, "result": {"id": 950, "username": "n_bot",
                                                                   "can_connect_to_business": True}})
        return httpx.Response(200, json={"ok": True, "result": True})

    tg.TRANSPORT = httpx.MockTransport(handler)
    await db.upsert_user(50, "N", None)
    await db.add_consent(50, settings.consent_version)
    tid = await db.create_tenant(50)
    await pbot.handle_update({"update_id": 1, "managed_bot": {
        "user": {"id": 50}, "bot": {"id": 950, "username": "n_bot"}}})
    t = await db.get_tenant(tid)
    assert t["bot_id"] == 950 and t["managed"] == 1
    assert ("setManagedBotAccessSettings", {"user_id": 950, "is_access_restricted": True}) in calls
    assert "950:managed" in (tenants.tenant_dir(tid) / ".env").read_text(encoding="utf-8")

    # Managed update from a stranger with no tenant: ignored, no token fetched.
    n = len(calls)
    await pbot.handle_update({"update_id": 2, "managed_bot": {"user": {"id": 99999}, "bot": {"id": 1}}})
    assert all(m != "getManagedBotToken" for m, _ in calls[n:])

    # Credit warning fires once below 20 %.
    await db.update_tenant(tid, status="running", or_key_hash="hw")
    openrouter.TRANSPORT = httpx.MockTransport(lambda r: httpx.Response(
        200, json={"data": {"limit": 10, "usage": 9, "limit_remaining": 1}}))
    calls.clear()
    await pbot.check_credit_once()
    await pbot.check_credit_once()
    assert [m for m, _ in calls].count("sendMessage") == 1
```

- [ ] **Step 2: Run to verify it fails**

Expected: FAIL — `AttributeError: module 'hosting.bot' has no attribute 'handle_update'`.

- [ ] **Step 3: Implement `hosting/bot.py`**

```python
"""Platform bot: receives managed-bot creations, answers /start, warns on low credit."""
from __future__ import annotations

import asyncio
import logging

from . import db, openrouter, tenants, tg
from .config import settings

log = logging.getLogger(__name__)
CREDIT_CHECK_SECONDS = 1800


async def _say(chat_id: int, text: str) -> None:
    try:
        await tg.call(settings.platform_bot_token, "sendMessage", chat_id=chat_id, text=text)
    except tg.TelegramError as e:
        log.warning("platform bot DM failed: %s", e)


async def on_managed_bot(creator_id: int, bot: dict) -> None:
    t = await db.get_tenant_by_owner(creator_id)
    if t is None:
        log.warning("managed bot %s from user %s with no tenant; ignored", bot.get("id"), creator_id)
        return
    token = await tg.call(settings.platform_bot_token, "getManagedBotToken", user_id=bot["id"])
    try:
        await tenants.attach_bot(t, token, managed=True)
    except db.BotTaken:
        log.warning("managed bot %s already attached elsewhere", bot.get("id"))
        return
    # Only the owner can open the bot's DM (where the owner commands live).
    await tg.call(settings.platform_bot_token, "setManagedBotAccessSettings",
                  user_id=bot["id"], is_access_restricted=True)


async def handle_update(u: dict) -> None:
    if mb := u.get("managed_bot"):
        await on_managed_bot(mb["user"]["id"], mb["bot"])
        return
    msg = u.get("message") or {}
    if (msg.get("text") or "").startswith("/start"):
        await _say(msg["chat"]["id"], f"برای ساخت منشی خودتان وارد سایت شوید:\n{settings.public_url}")


async def check_credit_once() -> None:
    for t in await db.list_tenants():
        if t["status"] != "running" or not t["or_key_hash"]:
            continue
        try:
            info = await openrouter.get_key(t["or_key_hash"])
        except openrouter.OpenRouterError as e:
            log.warning("credit check failed for tenant %s: %s", t["id"], e)
            continue
        limit = float(info.get("limit") or 0)
        remaining = float(info.get("limit_remaining") or 0)
        if limit and remaining < 0.2 * limit and t["warned_at_limit"] != limit:
            await _say(t["owner_tg_id"], f"اعتبار منشی شما رو به اتمام است (باقی\u200cمانده: ${remaining:.2f}). "
                                         f"برای شارژ: {settings.public_url}/account")
            await db.update_tenant(t["id"], warned_at_limit=limit)


async def _credit_loop() -> None:
    while True:
        try:
            await check_credit_once()
        except Exception:  # noqa: BLE001 — keep the loop alive
            log.exception("credit loop")
        await asyncio.sleep(CREDIT_CHECK_SECONDS)


async def run_platform_bot() -> None:
    credit = asyncio.create_task(_credit_loop())
    offset = 0
    try:
        while True:
            try:
                updates = await tg.call(settings.platform_bot_token, "getUpdates", offset=offset,
                                        timeout=30, allowed_updates=["message", "managed_bot"])
            except Exception as e:  # noqa: BLE001
                log.warning("getUpdates failed: %s", e)
                await asyncio.sleep(5)
                continue
            for u in updates:
                offset = u["update_id"] + 1
                try:
                    await handle_update(u)
                except Exception:  # noqa: BLE001 — one bad update must not stop the loop
                    log.exception("update %s failed", u.get("update_id"))
    finally:
        credit.cancel()
```

- [ ] **Step 4: Run to verify it passes**

Run: `PYTHONIOENCODING=utf-8 uv run python scripts/smoke_hosting.py && uv run python -c "import hosting.__main__; print('OK')"`
Expected: all `ok`, `OK`. (The import needs a `hosting.env`; for this check run with the env vars from the smoke script exported, or skip it and rely on the smoke import of `hosting.app`.)

- [ ] **Step 5: Checkpoint**

---

## Wave 3 — deploy

### Task 13: Deploy files and staging end-to-end

**Files:**
- Create: `hosting/ecosystem.config.cjs`, `hosting/Caddyfile.example`, `hosting/README.md`
- Modify: `CLAUDE.md` (Layout: add `hosting/`; Smoke tests: add `smoke_hosting.py`; one foot-gun line: "`hosting` never `platform`")

- [ ] **Step 1:** `hosting/ecosystem.config.cjs` — one app `tg-hosting`, `script` = `<repo>/.venv/bin/python`, `args: "-m hosting"`, `interpreter: "none"`, `cwd` = repo root, logs `./logs/hosting-*.log`, `max_memory_restart: "300M"`.
- [ ] **Step 2:** `hosting/Caddyfile.example`:

```
example.com {
    encode gzip
    reverse_proxy 127.0.0.1:8700
}
```

- [ ] **Step 3:** `hosting/README.md` (English, short): BotFather setup for the platform bot (Bot Management Mode on, Login Widget client id/secret + allowed URL `https://<domain>/auth/callback`), OpenRouter management key, `hosting.env`, `uv sync`, `pm2 start hosting/ecosystem.config.cjs`, `pm2 save`, Caddy. Admin flow: record payments at `/admin`.
- [ ] **Step 4: Full offline verification**

Run:
```bash
PYTHONIOENCODING=utf-8 uv run python scripts/smoke_hosting.py
PYTHONIOENCODING=utf-8 uv run python scripts/smoke_dashboard.py
uv run python scripts/smoke_core.py
uv run python scripts/smoke_setup.py
uv run python -c "import secretary.__main__; print('OK')"
git diff --stat main...HEAD
```
Expected: all pass; diff shows no removed tests or line-ending churn.

- [ ] **Step 5: Staging end-to-end (manual, with the user)** — on a staging VPS outside Iran, never turkey_vps prod. Ask the user for the server and domain first. Checklist:
  1. Log in with Telegram on a phone; land on consent.
  2. Accept; create bot with one tap; page shows the bot attached. Confirm the `id` claim equals the real Telegram user id (spec's unconfirmed item) and the `managed_bot` update shape.
  3. If "Secretary Mode" prompt appears, toggle it in BotFather; "check again" clears it.
  4. Fill profile; land on payment instructions.
  5. As admin record $1; tenant starts (`pm2 ls` shows `tgs-1` online).
  6. In Telegram: Settings > Chat Automation > pick the bot; account page flips to connected.
  7. From a second account, message the user; a reply arrives after the delay.
  8. Open `/app/`: Persian dashboard works; change reply delay; it applies.
  9. Lower the key limit to the current usage in OpenRouter; next message → owner gets the "credit used up" DM once.
  10. Delete account; `pm2 ls` has no `tgs-1`; tenant dir gone; OpenRouter key disabled; the managed bot's old token fails `getMe`.
- [ ] **Step 6: Checkpoint**
