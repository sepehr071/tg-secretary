# Setup Wizard + Web Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A validated terminal setup wizard (`uv run python -m secretary.setup`) and a loopback-only web dashboard served by the bot process, covering settings, status, contacts/prompts/memory and HITL drafts.

**Architecture:** `secretary/setup.py` is a standalone sync wizard. It uses httpx and python-dotenv and does not import `secretary.config` until `.env` exists. `secretary/dashboard/` is a FastAPI app, built by `create_app(bot, request_stop)` and served by a `uvicorn.Server` task inside `secretary/__main__.py`'s asyncio loop. It binds `127.0.0.1` and reuses `db`, `commands` and `prompts` helpers directly. Login works only through one-time links stored as sha256 hashes in `bot_state`.

**Tech Stack:** Python 3.13, FastAPI 0.142 / Starlette 1.7, uvicorn 0.54, Jinja2, httpx, python-dotenv, aiosqlite. All are already in `pyproject.toml`.

**Spec:** `docs/superpowers/specs/2026-10-04-setup-wizard-dashboard-design.md`

## Global Constraints

- No new dependencies. Everything used is already in `pyproject.toml`.
- Dashboard binds `127.0.0.1` only. No host setting exists or gets added.
- Login token TTL 1 hour (`LOGIN_TTL = 3600`), session TTL 7 days (`SESSION_TTL = 7 * 86400`), cookie `tgs_session` (`HttpOnly`, `SameSite=Strict`, `Path=/`, not `Secure`).
- `bot_state` key prefixes: `dash_login:` and `dash_session:`, each followed by the sha256 hex of the token. Value = expiry epoch (int as string).
- Default dashboard port `8780` (`DASHBOARD_PORT`), `DASHBOARD_ENABLED=true`.
- Raw `TG_BOT_TOKEN` / `OPENROUTER_API_KEY` never appear in rendered HTML or terminal output; show them as `mask()` → `1234…abcd`.
- Live `bot_state` keys must be exactly the ones `/commands` use: `paused`, `approval_mode`, `innercircle_gate`, `voice_override`, `quiet_start`, `quiet_end`, `delay_override`, `away_delay_override`, `cooldown_override`.
- UI copy in English. Every textarea/input that can hold Persian gets `dir="auto"`.
- Files written from textareas: UTF-8, LF line endings (normalize `\r\n`).
- Owner filters stay: `commands._active_conn_id()` and `db.get_connection` filter on `settings.owner_user_id`; new connection queries do too.
- Editing rules from the owner: never `sed -i` across files; never write code files via heredoc/`printf`. Use the Edit/Write tools.
- Windows: prefix runs that print Persian with `PYTHONIOENCODING=utf-8`.
- **Never run `uv run python -m secretary` with the real `.env` on this machine.** The server bot polls the same token: you get a 409 conflict and could auto-reply in real chats. Verify through the smoke scripts and the scratchpad harness in Task 11.
- Commits: only when the owner has asked for them in this run. If asked, first create branch `feat/setup-dashboard` (never commit on `main`), write the message with the `caveman-commit` skill, and add no Claude attribution of any kind.

## Review Focus

1. **Tunnel on a different local port** (`ssh -L 9000:127.0.0.1:8780`): the browser sends `Host: 127.0.0.1:9000`, and login must still work. Test lives in Task 4.
2. **Persian text with CRLF from a textarea**: the saved prompt file must be UTF-8 with LF only, and must survive Windows' cp1252 default. Tests live in Task 8 (contact prompt file) and Task 9 (prompts page).
3. **Network down while saving a token/key in Settings**: the user sees an error flash, `.env` is unchanged, and there is no 500. Test lives in Task 7.
4. **OpenRouter credit lookup fails**: the Status page still renders, showing "unavailable". Test lives in Task 6.
5. **Hand-copied `.env.example` placeholders on a wizard re-run**: `replace-with-...` and `123456789` count as unset and are not offered as the current value. Test lives in Task 1.

---

## File map

| File | Responsibility |
|---|---|
| `secretary/setup.py` (new) | Wizard: pure helpers (`mask`, `new_code`, `find_owner`, `ssh_hint`, `merge_env`, `real_value`), sync network checks (`tg_get_me`, `or_key_info`, `or_model_ids`, `tg_get_updates`, `tg_send`, `wait_for_owner`), interactive `main()`. The dashboard reuses its helpers. |
| `secretary/dashboard/__init__.py` (new, empty) | Package marker. Must stay empty: `commands.py` imports `dashboard.auth`, so `__init__` must not import `app`/`pages`, which would be circular. |
| `secretary/dashboard/auth.py` (new) | Token/session lifecycle + `host_ok` / `origin_ok`. |
| `secretary/dashboard/web.py` (new) | Jinja `templates`, `render()`, `back()` redirect-with-flash, `mask`/`ts` filters. |
| `secretary/dashboard/app.py` (new) | `create_app`, guard middleware, `/login`, `/logout`, router includes, `make_server`, `run_server`. |
| `secretary/dashboard/pages.py` (new) | Status `/`, Settings `/settings*`, `/restart`. |
| `secretary/dashboard/contacts.py` (new) | Contacts list/detail/memory + Prompts editor. |
| `secretary/dashboard/drafts.py` (new) | HITL drafts page. |
| `secretary/dashboard/templates/*.html` (new) | base, login, status, settings, restarting, contacts, contact, prompts, drafts. |
| `secretary/dashboard/static/style.css` (new) | Minimal styling. |
| `secretary/db.py` | `pop_state`, `purge_expired_state`, `count_open_pending`, `get_stats`, `recent_bot_replies`, `list_connections`, `list_chats`, `list_open_pending`. |
| `secretary/commands.py` | `on_stats` uses `db.get_stats`; `/dashboard`; `_resolve_pending(bot, ...)`. |
| `secretary/config.py`, `.env.example` | `dashboard_enabled`, `dashboard_port`. |
| `secretary/__main__.py` | Start/stop the dashboard task. |
| `scripts/smoke_setup.py` (new) | Offline asserts for wizard helpers. |
| `scripts/smoke_dashboard.py` (new) | Offline asserts for db helpers + every dashboard route (httpx `ASGITransport`, same event loop as the DB). |
| `scripts/setup-ubuntu.sh`, `README.md`, `CLAUDE.md` | Call the wizard; document setup + dashboard. |

---

## Phase 1: Setup wizard

### Task 1: Wizard helpers + network checks

**Files:**
- Create: `secretary/setup.py`
- Create: `scripts/smoke_setup.py`

**Interfaces:**
- Consumes: nothing from the project (must not import `secretary.config`).
- Produces (used by Tasks 2, 5, 7):
  - `ENV_PATH: Path = Path(".env")`, `EXAMPLE_PATH: Path = Path(".env.example")`, `DEFAULT_MODEL = "openai/gpt-5.5"`, `TOKEN_RE`
  - `class SetupError(Exception)`, `class PollConflict(SetupError)`
  - `mask(secret: str) -> str`
  - `real_value(value: str | None) -> str`
  - `new_code() -> str`
  - `find_owner(updates: list[dict], code: str) -> dict | None`
  - `ssh_hint(port: int, env: Mapping[str, str]) -> str | None`
  - `merge_env(env_path: Path, example_path: Path, values: Mapping[str, str]) -> Path | None` (returns backup path)
  - `tg_get_me(token: str) -> dict` (raises `SetupError`; `httpx.TransportError` propagates)
  - `or_key_info(key: str, timeout: float = 15) -> dict` (raises `SetupError`; `httpx.TransportError` propagates)
  - `or_model_ids() -> set[str] | None`
  - `tg_get_updates(token: str, offset: int, timeout: int) -> list[dict]` (raises `PollConflict` on 409)
  - `tg_send(token: str, chat_id: int, text: str) -> None`
  - `wait_for_owner(token: str, code: str, seconds: int = 600) -> dict | None`

- [ ] **Step 1: Write the failing smoke script** `scripts/smoke_setup.py`:

```python
"""Offline checks for the setup wizard helpers. No network, no .env needed.

    uv run python scripts/smoke_setup.py
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import dotenv_values  # noqa: E402

from secretary.setup import (  # noqa: E402
    TOKEN_RE, find_owner, mask, merge_env, new_code, real_value, ssh_hint,
)

# Masking never reveals the middle of a secret.
assert mask("123456:ABCDEFGHIJKLMNOP") == "1234…MNOP"
assert mask("short") == "****"

# .env.example placeholders count as unset on a re-run.
assert real_value("replace-with-your-telegram-bot-token") == ""
assert real_value("123456789") == ""
assert real_value(None) == ""
assert real_value(" 42 ") == "42"

# Codes: 6 chars from an unambiguous, deep-link-safe alphabet.
code = new_code()
assert len(code) == 6 and code.isalnum() and not set(code) & set("0O1IL")

# Owner match: only a private-chat "/start <code>" counts.
updates = [
    {"update_id": 1, "message": {"chat": {"type": "group"}, "from": {"id": 9}, "text": f"/start {code}"}},
    {"update_id": 2, "message": {"chat": {"type": "private"}, "from": {"id": 8}, "text": "/start WRONG1"}},
    {"update_id": 3, "message": {"chat": {"type": "private"}, "from": {"id": 7, "first_name": "Sam"},
                                 "text": f"/start {code}"}},
    {"update_id": 4, "business_message": {"text": "hi"}},
]
assert find_owner(updates, code) == {"id": 7, "first_name": "Sam"}
assert find_owner(updates[:2], code) is None

# ssh hint only over SSH, using the server-side address.
assert ssh_hint(8780, {}) is None
assert ssh_hint(8780, {"SSH_CONNECTION": "1.2.3.4 5555 10.0.0.9 22", "USER": "deploy"}) == \
    "ssh -L 8780:127.0.0.1:8780 deploy@10.0.0.9"

# Token shape gate keeps junk (and path tricks) out of the getMe URL.
assert TOKEN_RE.match("123456:" + "A" * 35)
assert not TOKEN_RE.match("123/../x:" + "A" * 35)

# .env merge: fresh install starts from the example; a re-run backs up and keeps unknown keys.
d = Path(tempfile.mkdtemp())
example, env = d / ".env.example", d / ".env"
example.write_text("# comment\nTG_BOT_TOKEN=replace-with-x\nHISTORY_TURNS=12\n", encoding="utf-8")
assert merge_env(env, example, {"TG_BOT_TOKEN": "tok"}) is None
vals = dotenv_values(env)
assert vals["TG_BOT_TOKEN"] == "tok" and vals["HISTORY_TURNS"] == "12"
env.write_text(env.read_text(encoding="utf-8") + "CUSTOM=keep\n", encoding="utf-8")
backup = merge_env(env, example, {"OWNER_FIRST_NAME": "O'Brien"})
assert backup is not None and "CUSTOM=keep" in backup.read_text(encoding="utf-8")
vals = dotenv_values(env)
assert vals["CUSTOM"] == "keep" and vals["OWNER_FIRST_NAME"] == "O'Brien" and vals["TG_BOT_TOKEN"] == "tok"
assert "# comment" in env.read_text(encoding="utf-8")

print("smoke_setup OK")
```

- [ ] **Step 2: Run it, expect failure**

Run: `uv run python scripts/smoke_setup.py`
Expected: `ModuleNotFoundError: No module named 'secretary.setup'`

- [ ] **Step 3: Implement** `secretary/setup.py` (helpers + network part; Task 2 adds the interactive part to the same file):

```python
"""First-run setup wizard.

    uv run python -m secretary.setup

Validates the Telegram bot token (getMe), the OpenRouter key (/api/v1/key) and
proves who owns the bot with a one-time /start code, then writes .env. Safe to
re-run: current values are offered as defaults and the old .env is backed up.

Must not import secretary.config at module level: Settings() fails at import
when .env does not exist yet. The dashboard reuses the helpers below.
"""
from __future__ import annotations

import os
import re
import secrets
import shutil
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import httpx
from dotenv import set_key

ENV_PATH = Path(".env")
EXAMPLE_PATH = Path(".env.example")
TG_API = "https://api.telegram.org"
OR_API = "https://openrouter.ai/api/v1"
DEFAULT_MODEL = "openai/gpt-5.5"
TOKEN_RE = re.compile(r"^\d+:[A-Za-z0-9_-]{30,}$")
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I/L
OWNER_WAIT_SECONDS = 600
_PLACEHOLDERS = ("replace-with",)
_PLACEHOLDER_VALUES = {"123456789"}


class SetupError(Exception):
    """A check failed. The message is safe to show: it never contains a secret."""


class PollConflict(SetupError):
    """Telegram answered 409: another process is already polling this bot."""


# ----- pure helpers -----


def mask(secret: str) -> str:
    """Show only the first and last 4 characters of a secret."""
    return "****" if len(secret) < 12 else f"{secret[:4]}…{secret[-4:]}"


def real_value(value: str | None) -> str:
    """Treat .env.example placeholders as unset."""
    v = (value or "").strip()
    return "" if v.startswith(_PLACEHOLDERS) or v in _PLACEHOLDER_VALUES else v


def new_code() -> str:
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(6))


def find_owner(updates: list[dict[str, Any]], code: str) -> dict[str, Any] | None:
    """Return the `from` user of the first private-chat `/start <code>` message."""
    for update in updates:
        msg = update.get("message") or {}
        if (msg.get("chat") or {}).get("type") != "private":
            continue
        if (msg.get("text") or "").strip() == f"/start {code}":
            return msg.get("from")
    return None


def ssh_hint(port: int, env: Mapping[str, str]) -> str | None:
    """`ssh -L` command for reaching the dashboard, or None when not on SSH."""
    parts = env.get("SSH_CONNECTION", "").split()
    if len(parts) != 4:
        return None
    return f"ssh -L {port}:127.0.0.1:{port} {env.get('USER') or 'you'}@{parts[2]}"


def merge_env(env_path: Path, example_path: Path, values: Mapping[str, str]) -> Path | None:
    """Write `values` into env_path and keep every other line as-is.

    Backs up an existing file to .env.<timestamp>.bak first and returns the
    backup path. A fresh install (no backup, returns None) starts from the
    example so every documented key is present.
    """
    backup = None
    if env_path.exists():
        backup = env_path.with_name(f"{env_path.name}.{time.strftime('%Y%m%d-%H%M%S')}.bak")
        shutil.copy2(env_path, backup)
    elif example_path.exists():
        shutil.copy2(example_path, env_path)
    else:
        env_path.touch()
    for key, value in values.items():
        set_key(env_path, key, value)
    if os.name == "posix":
        env_path.chmod(0o600)
    return backup


# ----- network checks (sync; the dashboard runs them via asyncio.to_thread) -----


def _json(r: httpx.Response, service: str) -> dict[str, Any]:
    try:
        return r.json()
    except ValueError:
        raise SetupError(
            f"{service} answered HTTP {r.status_code} without JSON (a proxy blocking it?)"
        ) from None


def tg_get_me(token: str) -> dict[str, Any]:
    if not TOKEN_RE.match(token):
        raise SetupError("that doesn't look like a bot token (123456:ABC...)")
    r = httpx.get(f"{TG_API}/bot{token}/getMe", timeout=15)
    if r.status_code in (401, 404):
        raise SetupError("Telegram rejected this token")
    body = _json(r, "Telegram")
    if not body.get("ok"):
        raise SetupError(f"Telegram error: {body.get('description', r.status_code)}")
    return body["result"]


def or_key_info(key: str, timeout: float = 15) -> dict[str, Any]:
    r = httpx.get(f"{OR_API}/key", headers={"Authorization": f"Bearer {key}"}, timeout=timeout)
    if r.status_code == 401:
        raise SetupError("OpenRouter rejected this key")
    body = _json(r, "OpenRouter")
    if r.status_code != 200 or "data" not in body:
        # A 403 "Access denied by security policy" is a blocked network, not a bad key.
        raise SetupError(
            f"OpenRouter answered HTTP {r.status_code}: {str(body.get('error', ''))[:200]} "
            "(403 'Access denied' usually means set HTTPS_PROXY)"
        )
    return body["data"]


def or_model_ids() -> set[str] | None:
    """All model ids on OpenRouter, or None when the list can't be fetched."""
    try:
        r = httpx.get(f"{OR_API}/models", timeout=20)
        r.raise_for_status()
        return {m["id"] for m in r.json()["data"]}
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        return None


def tg_get_updates(token: str, offset: int, timeout: int) -> list[dict[str, Any]]:
    r = httpx.get(
        f"{TG_API}/bot{token}/getUpdates",
        params={"offset": offset, "timeout": timeout, "allowed_updates": '["message"]'},
        timeout=timeout + 15,
    )
    if r.status_code == 409:
        raise PollConflict("the bot is already running — stop it first: pm2 stop tg-secretary")
    body = _json(r, "Telegram")
    if not body.get("ok"):
        raise SetupError(f"Telegram error: {body.get('description', r.status_code)}")
    return body["result"]


def tg_send(token: str, chat_id: int, text: str) -> None:
    """Best-effort DM; a failure here must not stop setup."""
    try:
        httpx.post(f"{TG_API}/bot{token}/sendMessage",
                   json={"chat_id": chat_id, "text": text}, timeout=15)
    except httpx.HTTPError:
        pass


def wait_for_owner(token: str, code: str, seconds: int = OWNER_WAIT_SECONDS) -> dict[str, Any] | None:
    """Long-poll until someone sends `/start <code>`; None on timeout."""
    # ponytail: confirming the offset also drops updates the bot hasn't seen yet.
    # Harmless on first setup (no business chats connected yet); re-runs default to
    # keeping the current owner and skip this poll. Track seen ids if that changes.
    offset = 0
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        updates = tg_get_updates(token, offset, 25)
        if not updates:
            continue
        offset = updates[-1]["update_id"] + 1
        owner = find_owner(updates, code)
        if owner is not None:
            tg_get_updates(token, offset, 0)  # confirm, so the bot doesn't replay /start
            return owner
    return None
```

- [ ] **Step 4: Run it, expect pass**

Run: `uv run python scripts/smoke_setup.py`
Expected: `smoke_setup OK`

- [ ] **Step 5: Commit** (only if the owner asked for commits; see Global Constraints)

```bash
git add secretary/setup.py scripts/smoke_setup.py
git commit -m "<caveman-commit message>"
```

### Task 2: Interactive wizard + Ubuntu script + README setup

**Files:**
- Modify: `secretary/setup.py` (append the interactive part)
- Modify: `scripts/setup-ubuntu.sh` (remove the `ask_*`/`is_int`/`mask` helpers, which have no other users, and replace step 6)
- Modify: `README.md` (the `## Setup` section only)

**Interfaces:**
- Consumes: everything Task 1 produces.
- Produces: `main() -> int`, `NEXT_STEPS: str`, `SECRET_KEYS: set[str]`. Task 5 extends `main()` with the dashboard link.

- [ ] **Step 1: Append the interactive part** to `secretary/setup.py`. Add `import getpass` and `import sys` to the imports, and change `from collections.abc import Mapping` to `from collections.abc import Callable, Mapping`. Add `from dotenv import dotenv_values, set_key`, replacing the `set_key`-only import. Then append:

```python
# ----- interactive wizard -----

SECRET_KEYS = {"TG_BOT_TOKEN", "OPENROUTER_API_KEY"}
BUSINESS_HELP = """  ! Business Mode is off for this bot. Turn it on:
    @BotFather → /mybots → <your bot> → Bot Settings → Business Mode → Turn on"""
NEXT_STEPS = """
Next:
  1. Start the bot:  pm2 start ecosystem.config.cjs   (without pm2: uv run python -m secretary)
  2. In Telegram: Settings → Business → Chatbots → add {bot},
     and allow "reply to messages" and "read messages".
  3. Send /help to {bot} for the owner commands."""


def ask(label: str, current: str = "", secret: bool = False) -> str:
    """Prompt until non-empty; Enter keeps `current` when there is one."""
    shown = mask(current) if secret else current
    hint = f" [{shown}]" if current else ""
    read = getpass.getpass if secret else input
    while True:
        value = read(f"  {label}{hint}: ").strip()
        if value or current:
            return value or current
        print("  ! required")


def ask_yes(label: str, default: bool = True) -> bool:
    value = input(f"  {label} [{'Y/n' if default else 'y/N'}]: ").strip().lower()
    return default if not value else value.startswith("y")


def ask_owner_id() -> int:
    while True:
        value = ask("Your numeric Telegram user id (ask @userinfobot)")
        if value.isdigit():
            return int(value)
        print("  ! must be a number")


def _net(host: str, fn: Callable[..., Any], *args: Any) -> Any:
    """Run a network check. On a connection failure offer retry, or skip (returns None)."""
    while True:
        try:
            return fn(*args)
        except httpx.TransportError as e:
            print(f"  ! can't reach {host} ({type(e).__name__}) — check HTTPS_PROXY / network")
            if input("  [r]etry or [s]kip this check? [r]: ").strip().lower().startswith("s"):
                return None


def step_token(current: str) -> tuple[str, dict[str, Any] | None]:
    """Return (token, getMe result or None when the check was skipped)."""
    while True:
        token = ask("Bot token", current, secret=True)
        try:
            me = _net("api.telegram.org", tg_get_me, token)
        except SetupError as e:
            print(f"  ! {e}")
            current = ""
            continue
        if me is None:
            return token, None
        print(f"  ✓ @{me['username']}")
        while not me.get("can_connect_to_business"):
            print(BUSINESS_HELP)
            input("  press Enter to check again... ")
            again = _net("api.telegram.org", tg_get_me, token)
            if again is None:
                break  # check skipped
            me = again
        return token, me


def step_key(current: str) -> str:
    while True:
        key = ask("API key", current, secret=True)
        try:
            info = _net("openrouter.ai", or_key_info, key)
        except SetupError as e:
            print(f"  ! {e}")
            current = ""
            continue
        if info is not None:
            left = info.get("limit_remaining")
            print(f"  ✓ key works ({'no credit limit' if left is None else f'${left:.2f} left'})")
        return key


def step_owner(token: str, me: dict[str, Any] | None, current: str) -> tuple[int, str]:
    """Return (owner user id, Telegram first name or "")."""
    if current.isdigit() and ask_yes(f"Keep owner {current}?"):
        return int(current), ""
    if me is None:
        return ask_owner_id(), ""
    code = new_code()
    print("  Open this link on your own Telegram account and tap Start:")
    print(f"    https://t.me/{me['username']}?start={code}")
    print("  waiting up to 10 minutes... (Ctrl+C to type your user id instead)")
    try:
        while True:
            try:
                owner = _net("api.telegram.org", wait_for_owner, token, code)
            except PollConflict as e:
                print(f"  ! {e}")
                input("  press Enter to retry... ")
                continue
            except SetupError as e:
                print(f"  ! {e}")
                return ask_owner_id(), ""
            if owner is not None:
                break
            if not ask_yes("No /start arrived. Keep waiting?"):
                return ask_owner_id(), ""
    except KeyboardInterrupt:
        print()
        return ask_owner_id(), ""
    tg_send(token, owner["id"], "Verified — you are the owner of this secretary.")
    print(f"  ✓ {owner.get('first_name', '')} (id {owner['id']})")
    return int(owner["id"]), owner.get("first_name", "")


def step_model(current: str) -> str:
    ids = or_model_ids()
    if ids is None:
        print("  ! couldn't fetch the model list — not checking the id")
    while True:
        model = ask("Reply model (any id from https://openrouter.ai/models)", current)
        if ids is None or model in ids:
            return model
        print("  ! unknown model id")
        current = ""


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass
    cur = {k: real_value(v) for k, v in dotenv_values(ENV_PATH).items()} if ENV_PATH.exists() else {}
    print("tg-secretary setup" + (" — Enter keeps the value in [brackets]" if cur else ""))
    try:
        print("\n[1/4] Telegram bot token — @BotFather → /newbot")
        token, me = step_token(cur.get("TG_BOT_TOKEN", ""))
        print("\n[2/4] OpenRouter API key — https://openrouter.ai/keys")
        key = step_key(cur.get("OPENROUTER_API_KEY", ""))
        print("\n[3/4] Owner — prove this Telegram account owns the bot")
        owner_id, tg_name = step_owner(token, me, cur.get("OWNER_USER_ID", ""))
        print("\n[4/4] Name and model")
        first = ask("Your first name (used in the persona)", cur.get("OWNER_FIRST_NAME") or tg_name)
        model = step_model(cur.get("OPENROUTER_MODEL") or DEFAULT_MODEL)
        values = {
            "TG_BOT_TOKEN": token,
            "OPENROUTER_API_KEY": key,
            "OWNER_USER_ID": str(owner_id),
            "OWNER_FIRST_NAME": first,
            "OPENROUTER_MODEL": model,
        }
        print()
        for k, v in values.items():
            print(f"  {k:<20} {mask(v) if k in SECRET_KEYS else v}")
        if not ask_yes(f"Write these to {ENV_PATH}?"):
            print("aborted — nothing written")
            return 1
    except (KeyboardInterrupt, EOFError):
        print("\naborted — nothing written")
        return 1
    backup = merge_env(ENV_PATH, EXAMPLE_PATH, values)
    print(f"\n✓ {ENV_PATH} written" + (f" (old one saved as {backup.name})" if backup else ""))
    print(NEXT_STEPS.format(bot=f"@{me['username']}" if me else "your bot"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Import + smoke check**

Run: `uv run python -c "import secretary.setup as s; assert callable(s.main); print('OK')"` then `uv run python scripts/smoke_setup.py`
Expected: `OK`, then `smoke_setup OK`

- [ ] **Step 3: Ubuntu script.** In `scripts/setup-ubuntu.sh`:
  - Delete the helper functions `ask_required`, `ask_secret`, `ask_default`, `ask_yn`, `is_int`, `mask`. Their only users are in step 6.
  - Replace the whole `# ---------- 6. .env interactive ----------` section, through the closing `fi` of the `if [ -z "${SKIP_ENV:-}" ]` block, with:

```bash
# ---------- 6. configure .env (validated wizard; re-run safe) ----------
step "Running the setup wizard"
# The wizard polls the bot for the owner check; a running copy would conflict.
pm2 stop tg-secretary >/dev/null 2>&1 || true
uv run python -m secretary.setup
```

Run: `bash -n scripts/setup-ubuntu.sh && grep -c "ask_\|is_int\|mask" scripts/setup-ubuntu.sh`
Expected: no syntax error. The count is `0`, or it only matches text inside the `step`/echo strings; check each remaining hit by eye.

- [ ] **Step 4: README.** Read `README.md` `## Setup`. Replace steps 2–4 with a wizard-based flow (keep step 1 BotFather, step 5 Business connect, step 6 `/help`):

```markdown
2. Get an [OpenRouter](https://openrouter.ai/keys) API key.
3. Install and run the setup wizard:
   ```bash
   git clone https://github.com/sepehr071/tg-secretary.git
   cd tg-secretary
   uv sync
   uv run python -m secretary.setup
   ```
   The wizard checks the bot token (and that Business Mode is on) and the API key, proves you own the bot by having you tap a one-time `/start` link, and writes `.env`. Re-running it is safe: Enter keeps each current value and the old `.env` is backed up. On Ubuntu, `./scripts/setup-ubuntu.sh` installs everything and runs the wizard for you.
4. Start it: `pm2 start ecosystem.config.cjs`, or `uv run python -m secretary` without pm2.
```

Remove the "Get your numeric Telegram user id" step; the wizard finds the id.

- [ ] **Step 5: Manual check (owner, real bot).** This is interactive and needs real credentials; ask the owner to run it, or run it only with their go-ahead and the server bot stopped. Run `uv run python -m secretary.setup` in a scratch copy of the repo and confirm:
  - token check, then `@username`;
  - key check, then the credit line;
  - the deep link captures your id and a "Verified" DM arrives;
  - `.env` is written and a `.bak` is created on the second run;
  - Ctrl+C at a prompt prints "aborted — nothing written".

- [ ] **Step 6: Commit** (only if asked): `secretary/setup.py scripts/setup-ubuntu.sh README.md`

---

## Phase 2: Dashboard core

### Task 3: DB helpers + `/stats` refactor

**Files:**
- Modify: `secretary/db.py` (bot_state section + new "dashboard queries" section at the end)
- Modify: `secretary/commands.py:196-243` (`on_stats`)
- Create: `scripts/smoke_dashboard.py`

**Interfaces:**
- Produces (used by Tasks 4, 6, 8, 10):
  - `pop_state(key: str) -> str | None`
  - `purge_expired_state(prefix: str, now: int) -> int`
  - `count_open_pending() -> int`
  - `get_stats(now: int | None = None) -> dict[str, int]` (keys `total_chats`, `replies_today`, `replies_week`, `aborted`, `pending`)
  - `recent_bot_replies(limit: int) -> list[dict]` (`chat_id`, `content`, `created_at`)
  - `list_connections() -> list[dict]` (owner's rows, newest first)
  - `list_chats() -> list[dict]` (`chat_id`, `last_seen`, `msg_count`, `relationship`, `nickname`, `paused`, `tg_first_name`, `tg_last_name`, `tg_username`)
  - `list_open_pending() -> list[dict]`

- [ ] **Step 1: Write the failing smoke script** `scripts/smoke_dashboard.py`:

```python
"""Offline checks for the web dashboard and its DB helpers. No Telegram, no OpenRouter.

    uv run python scripts/smoke_dashboard.py
"""
import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

_tmp = Path(tempfile.mkdtemp())
TOKEN = "123456:SMOKEtokenSMOKEtokenSMOKEtoken99"
KEY = "sk-or-v1-smokekeysmokekeysmokekey77"
os.environ.update(
    TG_BOT_TOKEN=TOKEN,
    OPENROUTER_API_KEY=KEY,
    OWNER_USER_ID="111",
    DB_PATH=str(_tmp / "smoke.db"),
    PROMPTS_DIR=str(_tmp / "prompts"),
)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from secretary import db  # noqa: E402


async def check_db() -> None:
    # One-time pop: the second read gets nothing.
    await db.set_state("dash_login:x", "123")
    assert await db.pop_state("dash_login:x") == "123"
    assert await db.pop_state("dash_login:x") is None

    # Purge drops only expired rows under the exact prefix ('_' is not a wildcard).
    now = int(time.time())
    await db.set_state("dash_session:old", str(now - 1))
    await db.set_state("dash_session:new", str(now + 60))
    await db.set_state("dashXsession:old", str(now - 1))
    assert await db.purge_expired_state("dash_session:", now) == 1
    assert await db.get_state("dash_session:new") and await db.get_state("dashXsession:old")

    # Stats, recent replies, owner-only connections, chats, open drafts.
    await db.upsert_connection(conn_id="c1", owner_user_id=111, owner_chat_id=111,
                               can_reply=True, is_enabled=True, rights={"can_reply": True})
    await db.upsert_connection(conn_id="stranger", owner_user_id=999, owner_chat_id=999,
                               can_reply=True, is_enabled=True, rights=None)
    await db.append_message(conn_id="c1", chat_id=5, role="user", content="hi")
    await db.append_message(conn_id="c1", chat_id=5, role="assistant", content="hey", via_bot=True)
    await db.create_pending(conn_id="c1", chat_id=5, contact_name="Sam", contact_msg="hi", draft="yo")
    await db.create_pending(conn_id="c1", chat_id=5, contact_name="Sam", contact_msg="hi",
                            draft="old", ttl_seconds=-1)
    s = await db.get_stats()
    assert (s["total_chats"], s["replies_today"], s["replies_week"], s["pending"]) == (1, 1, 1, 1)
    assert await db.count_open_pending() == 1
    assert [r["content"] for r in await db.recent_bot_replies(5)] == ["hey"]
    assert [c["conn_id"] for c in await db.list_connections()] == ["c1"]
    assert [c["chat_id"] for c in await db.list_chats()] == [5]
    assert [p["draft"] for p in await db.list_open_pending()] == ["yo"]


async def main() -> None:
    await db.init_db()
    await check_db()
    await db.close_db()
    print("smoke_dashboard OK")


asyncio.run(main())
```

- [ ] **Step 2: Run it, expect failure**

Run: `uv run python scripts/smoke_dashboard.py`
Expected: `AttributeError: module 'secretary.db' has no attribute 'pop_state'`

- [ ] **Step 3: Implement.** In `secretary/db.py`, append to the `bot_state` section:

```python
async def pop_state(key: str) -> str | None:
    """Delete a bot_state row and return its value in one statement, so a
    one-time token can be redeemed only once."""
    db = _db()
    cur = await db.execute("DELETE FROM bot_state WHERE key = ? RETURNING value", (key,))
    row = await cur.fetchone()
    await db.commit()
    return row["value"] if row else None


async def purge_expired_state(prefix: str, now: int) -> int:
    """Drop rows under `prefix` whose value (an expiry epoch) is in the past."""
    db = _db()
    cur = await db.execute(
        "DELETE FROM bot_state WHERE substr(key, 1, ?) = ? AND CAST(value AS INTEGER) < ?",
        (len(prefix), prefix, now),
    )
    await db.commit()
    return cur.rowcount
```

Then append a new section at the end of the file:

```python
# ---------------------------------------------------------------------------
# dashboard queries
# ---------------------------------------------------------------------------


async def count_open_pending() -> int:
    cur = await _db().execute(
        "SELECT COUNT(*) AS n FROM pending_replies WHERE status = 'pending' AND expires_at > ?",
        (int(time.time()),),
    )
    row = await cur.fetchone()
    return int(row["n"]) if row else 0


async def get_stats(now: int | None = None) -> dict[str, int]:
    """Counts behind /stats and the dashboard Status page."""
    db = _db()
    now = now or int(time.time())

    async def one(sql: str, *args: Any) -> int:
        cur = await db.execute(sql, args)
        row = await cur.fetchone()
        return int(row["n"]) if row else 0

    bot_since = (
        "SELECT COUNT(*) AS n FROM messages "
        "WHERE role='assistant' AND via_bot=1 AND created_at >= ?"
    )
    return {
        "total_chats": await one("SELECT COUNT(DISTINCT chat_id) AS n FROM messages"),
        "replies_today": await one(bot_since, now - 86400),
        "replies_week": await one(bot_since, now - 7 * 86400),
        # Approximate aborts: user msg with no via_bot=1 assistant reply within 5 min.
        "aborted": await one(
            """
            SELECT COUNT(*) AS n FROM messages u
            WHERE u.role = 'user'
              AND NOT EXISTS (
                SELECT 1 FROM messages a
                WHERE a.conn_id = u.conn_id AND a.chat_id = u.chat_id
                  AND a.role = 'assistant' AND a.via_bot = 1
                  AND a.created_at BETWEEN u.created_at AND u.created_at + 300
              )
            """
        ),
        "pending": await count_open_pending(),
    }


async def recent_bot_replies(limit: int) -> list[dict[str, Any]]:
    cur = await _db().execute(
        "SELECT chat_id, content, created_at FROM messages "
        "WHERE role = 'assistant' AND via_bot = 1 ORDER BY id DESC LIMIT ?",
        (limit,),
    )
    return [dict(r) for r in await cur.fetchall()]


async def list_connections() -> list[dict[str, Any]]:
    """The owner's business connections, newest first (never a stranger's)."""
    cur = await _db().execute(
        "SELECT * FROM connections WHERE owner_user_id = ? ORDER BY updated_at DESC",
        (settings.owner_user_id,),
    )
    return [dict(r) for r in await cur.fetchall()]


async def list_chats() -> list[dict[str, Any]]:
    """Every chat that has messaged the owner, newest first, with its contact row."""
    cur = await _db().execute(
        """
        SELECT m.chat_id, MAX(m.created_at) AS last_seen, COUNT(*) AS msg_count,
               o.relationship, o.nickname, o.paused,
               o.tg_first_name, o.tg_last_name, o.tg_username
        FROM messages m
        LEFT JOIN contact_overrides o ON o.chat_id = m.chat_id AND o.conn_id = m.conn_id
        WHERE m.role = 'user'
        GROUP BY m.chat_id
        ORDER BY last_seen DESC
        """
    )
    return [dict(r) for r in await cur.fetchall()]


async def list_open_pending() -> list[dict[str, Any]]:
    cur = await _db().execute(
        "SELECT * FROM pending_replies WHERE status = 'pending' AND expires_at > ? "
        "ORDER BY created_at",
        (int(time.time()),),
    )
    return [dict(r) for r in await cur.fetchall()]
```

Then replace the body of `commands.on_stats` after its owner guard with:

```python
    s = await db.get_stats()
    text = (
        f"total chats: {s['total_chats']}\n"
        f"replies today: {s['replies_today']}\n"
        f"replies this week: {s['replies_week']}\n"
        f"aborted (approx, no reply within 5min): {s['aborted']}"
    )
    await _reply(update, text)
```

- [ ] **Step 4: Run checks**

Run: `uv run python scripts/smoke_dashboard.py && uv run python scripts/smoke_core.py`
Expected: `smoke_dashboard OK` then `smoke_core OK`

- [ ] **Step 5: Commit** (only if asked): `secretary/db.py secretary/commands.py scripts/smoke_dashboard.py`

### Task 4: Auth, app skeleton, login/logout

**Files:**
- Create: `secretary/dashboard/__init__.py` (empty file)
- Create: `secretary/dashboard/auth.py`, `secretary/dashboard/web.py`, `secretary/dashboard/app.py`
- Create: `secretary/dashboard/templates/base.html`, `secretary/dashboard/templates/login.html`
- Create: `secretary/dashboard/static/style.css`
- Modify: `scripts/smoke_dashboard.py`

**Interfaces:**
- Consumes: `db.set_state/get_state/pop_state/purge_expired_state/count_open_pending` (Task 3), `setup.mask`, `setup.ENV_PATH` (Task 1).
- Produces:
  - `auth.LOGIN_TTL`, `auth.SESSION_TTL`, `auth.COOKIE`
  - `auth.host_ok(host: str) -> bool`
  - `auth.origin_ok(origin: str | None, host: str) -> bool`
  - `auth.create_login_token(ttl: int = LOGIN_TTL) -> str` (async)
  - `auth.redeem_login_token(token: str) -> str | None` (async)
  - `auth.session_valid(session: str | None) -> bool` (async)
  - `auth.end_session(session: str | None) -> None` (async)
  - `web.templates`
  - `web.render(request, name, status_code=200, **context) -> HTMLResponse`
  - `web.back(url, msg="", err="", **params) -> RedirectResponse` (303)
  - `app.create_app(bot, request_stop, env_path=ENV_PATH) -> FastAPI`. `app.state` holds `bot`, `request_stop`, `env_path`, `started_at`; `request.state.pending` is set on authenticated requests.
  - `app.make_server(app, port) -> uvicorn.Server`
  - `app.run_server(server) -> None` (async)

- [ ] **Step 1: Extend the smoke script.** Add these imports and helpers below `from secretary import db`:

```python
import types  # noqa: E402
from http.cookies import SimpleCookie  # noqa: E402

import httpx  # noqa: E402

from secretary.dashboard import auth  # noqa: E402
from secretary.dashboard.app import create_app  # noqa: E402

BASE = "http://127.0.0.1:8780"
ORIGIN = {"origin": BASE}
ENV = _tmp / ".env"
STOPS: list[int] = []


class FakeBot:
    username = "smoke_bot"

    def __init__(self) -> None:
        self.sent: list[tuple[int, str, str | None]] = []

    async def send_message(self, chat_id, text, business_connection_id=None, **_):
        self.sent.append((chat_id, text, business_connection_id))
        return types.SimpleNamespace(message_id=len(self.sent))


BOT = FakeBot()


def client(app, base: str = BASE, **kw) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=base, **kw)


def session_from(r: httpx.Response) -> str:
    return SimpleCookie(r.headers["set-cookie"])[auth.COOKIE].value


async def authed(app) -> httpx.AsyncClient:
    """Client carrying a fresh session cookie and a same-origin Origin header."""
    async with client(app) as c:
        r = await c.post("/login", data={"token": await auth.create_login_token()}, headers=ORIGIN)
    return client(app, headers={"cookie": f"{auth.COOKIE}={session_from(r)}", **ORIGIN})


async def check_auth(app) -> None:
    async with client(app) as c:
        r = await c.get("/")
        assert r.status_code == 303 and r.headers["location"] == "/login"
        assert (await c.get("/login", headers={"host": "evil.example:8780"})).status_code == 400
        token = await auth.create_login_token()
        assert (await c.post("/login", data={"token": token})).status_code == 403  # no Origin
        r = await c.post("/login", data={"token": token}, headers=ORIGIN)
        assert r.status_code == 303
        session = session_from(r)
        assert await auth.session_valid(session)
        r = await c.post("/login", data={"token": token}, headers=ORIGIN)  # second use
        assert r.status_code == 401
        expired = await auth.create_login_token(ttl=-1)
        assert (await c.post("/login", data={"token": expired}, headers=ORIGIN)).status_code == 401
        r = await c.post("/logout", headers={"cookie": f"{auth.COOKIE}={session}", **ORIGIN})
        assert r.status_code == 303 and not await auth.session_valid(session)
    # Review focus 1: a tunnel on another local port still logs in.
    async with client(app, base="http://127.0.0.1:9000") as c:
        r = await c.post("/login", data={"token": await auth.create_login_token()},
                         headers={"origin": "http://127.0.0.1:9000"})
        assert r.status_code == 303
    assert auth.host_ok("[::1]:8780") and auth.host_ok("localhost")
    assert not auth.host_ok("localhost.evil.com:8780") and not auth.host_ok("")
```

and change `main()` to:

```python
async def main() -> None:
    await db.init_db()
    await check_db()
    app = create_app(BOT, lambda: STOPS.append(1), env_path=ENV)
    await check_auth(app)
    await db.close_db()
    print("smoke_dashboard OK")
```

- [ ] **Step 2: Run, expect failure**

Run: `uv run python scripts/smoke_dashboard.py`
Expected: `ModuleNotFoundError: No module named 'secretary.dashboard'`

- [ ] **Step 3: Implement.** Create the empty `secretary/dashboard/__init__.py`. Then:

`secretary/dashboard/auth.py`:

```python
"""Dashboard login: one-time links, hashed sessions, request guards.

Login tokens and sessions live in bot_state as sha256 hashes with an expiry
epoch as the value, so a copied DB holds no usable session and sessions
survive pm2 restarts.
"""
from __future__ import annotations

import hashlib
import secrets
import time

from .. import db

LOGIN_TTL = 3600
SESSION_TTL = 7 * 86400
COOKIE = "tgs_session"
LOGIN_PREFIX = "dash_login:"
SESSION_PREFIX = "dash_session:"
_LOCAL_NAMES = {"127.0.0.1", "localhost", "[::1]"}


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def host_ok(host: str) -> bool:
    """Loopback hostnames only, any port: blocks DNS rebinding, allows ssh -L 9000:..."""
    name = host if host.endswith("]") else host.rsplit(":", 1)[0]
    return name.lower() in _LOCAL_NAMES


def origin_ok(origin: str | None, host: str) -> bool:
    """POSTs must come from a page this dashboard served (CSRF guard)."""
    return origin == f"http://{host}"


async def create_login_token(ttl: int = LOGIN_TTL) -> str:
    token = secrets.token_urlsafe(32)
    await db.set_state(LOGIN_PREFIX + _hash(token), str(int(time.time()) + ttl))
    return token


async def redeem_login_token(token: str) -> str | None:
    """Spend a login token (works once). Returns a new session token, or None."""
    now = int(time.time())
    await db.purge_expired_state(LOGIN_PREFIX, now)
    await db.purge_expired_state(SESSION_PREFIX, now)
    expires = await db.pop_state(LOGIN_PREFIX + _hash(token))
    if expires is None or int(expires) < now:
        return None
    session = secrets.token_urlsafe(32)
    await db.set_state(SESSION_PREFIX + _hash(session), str(now + SESSION_TTL))
    return session


async def session_valid(session: str | None) -> bool:
    if not session:
        return False
    expires = await db.get_state(SESSION_PREFIX + _hash(session))
    return expires is not None and int(expires) > time.time()


async def end_session(session: str | None) -> None:
    if session:
        await db.pop_state(SESSION_PREFIX + _hash(session))
```

`secretary/dashboard/web.py`:

```python
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
```

`secretary/dashboard/app.py`:

```python
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
from ..setup import ENV_PATH
from . import auth
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
        resp.set_cookie(auth.COOKIE, session, max_age=auth.SESSION_TTL,
                        httponly=True, samesite="strict", path="/")
        return resp

    @app.post("/logout")
    async def logout(request: Request):
        await auth.end_session(request.cookies.get(auth.COOKIE))
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie(auth.COOKIE, path="/")
        return resp

    return app


class _Server(uvicorn.Server):
    def capture_signals(self):  # type: ignore[override]
        # The bot owns SIGINT/SIGTERM via loop.add_signal_handler; uvicorn must not replace them.
        return contextlib.nullcontext()


def make_server(app: FastAPI, port: int) -> uvicorn.Server:
    config = uvicorn.Config(app, host="127.0.0.1", port=port, access_log=False,
                            log_config=None, log_level="warning")
    return _Server(config)


async def run_server(server: uvicorn.Server) -> None:
    """Serve until server.should_exit. A busy port logs an error instead of killing the bot."""
    try:
        await server.serve()
    except SystemExit:  # uvicorn calls sys.exit(1) when it can't bind
        log.error("dashboard not started: port %s is busy; the bot keeps running", server.config.port)
```

`secretary/dashboard/templates/base.html`:

```html
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>tg-secretary</title>
<link rel="stylesheet" href="/static/style.css">
</head>
<body>
{% if request.state.pending is defined %}
<nav>
  <strong>tg-secretary</strong>
  <a href="/">Status</a>
  <a href="/settings">Settings</a>
  <a href="/contacts">Contacts</a>
  <a href="/prompts">Prompts</a>
  <a href="/drafts">Drafts{% if request.state.pending %} ({{ request.state.pending }}){% endif %}</a>
  <form method="post" action="/logout"><button>Log out</button></form>
</nav>
{% endif %}
{% if request.query_params.msg %}<p class="flash ok" dir="auto">{{ request.query_params.msg }}</p>{% endif %}
{% if err or request.query_params.err %}<p class="flash err" dir="auto">{{ err or request.query_params.err }}</p>{% endif %}
<main>{% block content %}{% endblock %}</main>
</body>
</html>
```

`secretary/dashboard/templates/login.html`:

```html
{% extends "base.html" %}
{% block content %}
<h1>Log in</h1>
<p>Send <code>/dashboard</code> to your bot in Telegram to get a one-time login link.</p>
<form method="post" action="/login" id="login" class="row">
  <input name="token" id="token" placeholder="token from the link" autocomplete="off" required>
  <button>Log in</button>
</form>
<script>
  // The token rides in the URL fragment so it never reaches a server log; submit it once.
  const t = new URLSearchParams(location.hash.slice(1)).get("t");
  if (t) {
    history.replaceState(null, "", "/login");
    document.getElementById("token").value = t;
    document.getElementById("login").submit();
  }
</script>
{% endblock %}
```

`secretary/dashboard/static/style.css`:

```css
:root { color-scheme: light dark; --muted: #888; --line: #8884; --bad: #c0392b; --good: #2e8b57; }
* { box-sizing: border-box; }
body { margin: 0 auto; max-width: 980px; padding: 16px; font: 15px/1.5 system-ui, sans-serif; }
nav { display: flex; flex-wrap: wrap; gap: 14px; align-items: center; padding-bottom: 12px;
      border-bottom: 1px solid var(--line); margin-bottom: 16px; }
nav form { margin-left: auto; }
.card { border: 1px solid var(--line); border-radius: 8px; padding: 12px 16px; margin: 0 0 16px; }
label { display: block; margin: 8px 0; }
input:not([type=checkbox]), select, textarea { font: inherit; padding: 4px 6px; max-width: 100%; }
textarea { width: 100%; }
table { border-collapse: collapse; width: 100%; }
th, td { text-align: start; padding: 4px 8px; border-bottom: 1px solid var(--line); vertical-align: top; }
.row { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
.flash { padding: 8px 12px; border-radius: 6px; }
.flash.ok { background: #2e8b5722; }
.flash.err, .warn { background: #c0392b22; }
.bad { color: var(--bad); }
.good { color: var(--good); }
small { color: var(--muted); }
blockquote { margin: 8px 0; padding-inline-start: 10px; border-inline-start: 3px solid var(--line); }
pre { white-space: pre-wrap; }
```

- [ ] **Step 4: Run checks**

Run: `uv run python scripts/smoke_dashboard.py`
Expected: `smoke_dashboard OK`

- [ ] **Step 5: Commit** (only if asked): `secretary/dashboard/ scripts/smoke_dashboard.py`

### Task 5: Runtime wiring, `/dashboard` command, wizard link

**Files:**
- Modify: `secretary/config.py` (after `prompts_dir`)
- Modify: `.env.example` (append)
- Modify: `secretary/__main__.py:88-125`
- Modify: `secretary/commands.py` (imports, new `on_dashboard`, `HELP_TEXT` Tools block, `register`)
- Modify: `secretary/setup.py` (`main()` end + new `first_login_link`)
- Modify: `scripts/smoke_dashboard.py`

**Interfaces:**
- Consumes: `create_app`, `make_server`, `run_server`, `auth.create_login_token` (Task 4); `setup.ssh_hint` (Task 1).
- Produces:
  - `settings.dashboard_enabled: bool`, `settings.dashboard_port: int`
  - `commands.on_dashboard`
  - `setup.first_login_link() -> tuple[str, int] | None`

- [ ] **Step 1: Failing test for the busy port.** Add to `scripts/smoke_dashboard.py`:

```python
import socket  # noqa: E402

from secretary.dashboard.app import make_server, run_server  # noqa: E402


async def check_port_busy() -> None:
    # Spec: a taken port must not kill the bot; run_server logs and returns.
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 0))
    blocker.listen()
    port = blocker.getsockname()[1]
    server = make_server(create_app(BOT, lambda: None, env_path=ENV), port)
    await asyncio.wait_for(run_server(server), timeout=10)
    blocker.close()
```

and call `await check_port_busy()` in `main()` after `check_auth`. Also add `from secretary.config import settings  # noqa: E402` to the imports, and add this to `check_auth`:

```python
    assert settings.dashboard_enabled is True and settings.dashboard_port == 8780
```

Run: `uv run python scripts/smoke_dashboard.py`
Expected: FAIL: `AttributeError: 'Settings' object has no attribute 'dashboard_enabled'`. The busy-port check should already pass, because `run_server` exists from Task 4.

- [ ] **Step 2: Config.** In `secretary/config.py` add after `prompts_dir`:

```python

    # Owner web dashboard, served by the bot on 127.0.0.1 only (reach it over `ssh -L`).
    dashboard_enabled: bool = True
    dashboard_port: int = 8780
```

Append to `.env.example`:

```
# Web dashboard on 127.0.0.1 only. From your computer: ssh -L 8780:127.0.0.1:8780 user@server
# Send /dashboard to the bot for a one-time login link.
DASHBOARD_ENABLED=true
DASHBOARD_PORT=8780
```

- [ ] **Step 3: `__main__.py`.** Add `from .dashboard.app import create_app, make_server, run_server` to the imports. After `def _request_stop()` and the signal-handler loop, before `try: await stop.wait()`, add:

```python
    dash_server = None
    dash_task = None
    if settings.dashboard_enabled:
        dash_server = make_server(create_app(app.bot, _request_stop), settings.dashboard_port)
        dash_task = asyncio.create_task(run_server(dash_server))
        log.info("Dashboard on http://127.0.0.1:%d — send /dashboard to the bot for a login link",
                 settings.dashboard_port)
```

In the `finally:` block, right after `log.info("Shutting down…")`:

```python
        if dash_server is not None and dash_task is not None:
            dash_server.should_exit = True
            await dash_task
```

- [ ] **Step 4: `/dashboard` command.** In `secretary/commands.py`:
  - Change the telegram import to `from telegram import InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions, Update`.
  - Add `from .dashboard.auth import create_login_token`.
  - Add this before `def register`:

```python
async def on_dashboard(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Reply with a one-time dashboard login link (valid 1 hour)."""
    if not _is_owner(update) or update.effective_message is None:
        return
    if not settings.dashboard_enabled:
        await _reply(update, "dashboard is off (DASHBOARD_ENABLED=false in .env)")
        return
    port = settings.dashboard_port
    token = await create_login_token()
    await update.effective_message.reply_text(
        "One-time dashboard login, valid 1 hour:\n"
        f"http://127.0.0.1:{port}/login#t={token}\n\n"
        f"Bot on a server? Open a tunnel first:\nssh -L {port}:127.0.0.1:{port} <user>@<server>",
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )
```

  - In `register`: `app.add_handler(CommandHandler("dashboard", on_dashboard))`.
  - In `HELP_TEXT` under `Tools:`, add the line ` /dashboard — one-time login link for the web dashboard`.

- [ ] **Step 5: Wizard link.** In `secretary/setup.py` add above `main()`:

```python
def first_login_link() -> tuple[str, int] | None:
    """Mint a one-time dashboard login in the bot DB, now that .env exists.
    Returns (url, port), or None when the dashboard is disabled."""
    import asyncio

    from . import db
    from .config import settings
    from .dashboard.auth import create_login_token

    if not settings.dashboard_enabled:
        return None

    async def mint() -> str:
        await db.init_db()
        try:
            return await create_login_token()
        finally:
            await db.close_db()

    port = settings.dashboard_port
    return f"http://127.0.0.1:{port}/login#t={asyncio.run(mint())}", port
```

and at the end of `main()`, replace `return 0` with:

```python
    try:
        link = first_login_link()
    except Exception as e:  # noqa: BLE001 — .env is written; the link is a convenience
        print(f"\n! couldn't create a dashboard link ({type(e).__name__}); send /dashboard to the bot later")
        link = None
    if link:
        url, port = link
        print("\nDashboard (once the bot is running):")
        hint = ssh_hint(port, os.environ)
        if hint:
            print(f"  1. On your computer, open a tunnel:  {hint}")
            print(f"  2. Open (works once, within 1 hour):  {url}")
        else:
            print(f"  Open (works once, within 1 hour):  {url}")
        print("  Lost it? Send /dashboard to the bot.")
    return 0
```

- [ ] **Step 6: Run checks**

Run: `uv run python scripts/smoke_dashboard.py && uv run python scripts/smoke_setup.py && uv run python scripts/smoke_core.py && uv run python -c "import secretary.__main__; print('OK')"`
Expected: `smoke_dashboard OK`, `smoke_setup OK`, `smoke_core OK`, `OK`

- [ ] **Step 7: Commit** (only if asked): `secretary/config.py .env.example secretary/__main__.py secretary/commands.py secretary/setup.py scripts/smoke_dashboard.py`

### Task 6: Status page

**Files:**
- Create: `secretary/dashboard/pages.py`
- Create: `secretary/dashboard/templates/status.html`
- Modify: `secretary/dashboard/app.py` (include router)
- Modify: `scripts/smoke_dashboard.py`

**Interfaces:**
- Consumes: `db.list_connections/get_stats/recent_bot_replies/get_state_bool` (Task 3), `setup.or_key_info` (Task 1), `web.render` (Task 4).
- Produces: `pages.router` (APIRouter); Task 7 adds Settings to it.

- [ ] **Step 1: Failing test.** In `scripts/smoke_dashboard.py`, right after the imports, stub the network checks (the routes call them through the `setup` module, so stubs take effect):

```python
from secretary import setup  # noqa: E402

setup.or_key_info = lambda key, timeout=15: {"limit_remaining": 4.2, "usage": 1.0}
setup.or_model_ids = lambda: {"openai/gpt-5.5", "google/gemini-3.1-flash-lite", "a/b"}
setup.tg_get_me = lambda token: {"username": "new_bot", "can_connect_to_business": True}
```

Add:

```python
async def check_status(app) -> None:
    async with await authed(app) as c:
        r = await c.get("/")
        assert r.status_code == 200, r.text
        assert "@smoke_bot" in r.text and "$4.20 left" in r.text
        assert TOKEN not in r.text and KEY not in r.text
        # Review focus 4: a failing credit lookup must not break the page.
        real = setup.or_key_info

        def boom(key, timeout=15):
            raise httpx.ConnectError("down")

        setup.or_key_info = boom
        r = await c.get("/")
        setup.or_key_info = real
        assert r.status_code == 200 and "unavailable" in r.text
```

and call `await check_status(app)` in `main()` after `check_port_busy()`.

Run: `uv run python scripts/smoke_dashboard.py`
Expected: FAIL: `AssertionError` (GET `/` returns 404).

- [ ] **Step 2: Implement** `secretary/dashboard/pages.py`:

```python
"""Status and Settings pages."""
from __future__ import annotations

import asyncio
import json
import time

from fastapi import APIRouter, Request

from .. import db, setup
from ..config import settings
from .web import render

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
```

`secretary/dashboard/templates/status.html`:

```html
{% extends "base.html" %}
{% block content %}
<h1>Status</h1>
<section class="card">
  <p>Bot <strong>@{{ bot_username }}</strong> · up {{ uptime }} · model <code>{{ model }}</code></p>
  <p>
    Auto-reply: {% if paused %}<span class="bad">paused</span>{% else %}<span class="good">on</span>{% endif %}
    · Approval mode: {{ "on" if approval else "off" }}
    · Inner-circle gate: {{ "on" if innercircle else "off" }}
    · <a href="/settings">change</a>
  </p>
  <p>OpenRouter credit:
    {% if credit is none %}unavailable
    {% elif credit.limit_remaining is none %}no limit (used ${{ "%.2f"|format(credit.usage or 0) }})
    {% else %}${{ "%.2f"|format(credit.limit_remaining) }} left{% endif %}
  </p>
</section>
<section class="card">
  <h2>Business connection</h2>
  {% for c in connections %}
    <p>{{ "enabled" if c.is_enabled else "disabled" }}
    {% if c.missing %} · <span class="bad">missing rights: {{ c.missing|join(", ") }}</span>
      (Telegram → Settings → Business → Chatbots){% endif %}</p>
  {% else %}
    <p class="bad">Not connected. In Telegram open Settings → Business → Chatbots and add @{{ bot_username }}.</p>
  {% endfor %}
</section>
<section class="card">
  <h2>Activity</h2>
  <p>{{ stats.replies_today }} replies today · {{ stats.replies_week }} this week ·
     {{ stats.total_chats }} chats · <a href="/drafts">{{ stats.pending }} drafts waiting</a></p>
  <table>
    <tr><th>When</th><th>Chat</th><th>Reply</th></tr>
    {% for r in replies %}
    <tr><td>{{ r.created_at|ts }}</td><td><a href="/contacts/{{ r.chat_id }}">{{ r.chat_id }}</a></td>
        <td dir="auto">{{ r.content[:120] }}</td></tr>
    {% else %}<tr><td colspan="3">No bot replies yet.</td></tr>{% endfor %}
  </table>
</section>
{% endblock %}
```

In `app.py`, change `from . import auth` to `from . import auth, pages`, and add this at the end of `create_app` before `return app`:

```python
    app.include_router(pages.router)
```

- [ ] **Step 3: Run checks**

Run: `uv run python scripts/smoke_dashboard.py`
Expected: `smoke_dashboard OK`

- [ ] **Step 4: Commit** (only if asked): `secretary/dashboard/ scripts/smoke_dashboard.py`

### Task 7: Settings page + restart

**Files:**
- Modify: `secretary/dashboard/pages.py`
- Create: `secretary/dashboard/templates/settings.html`, `secretary/dashboard/templates/restarting.html`
- Modify: `scripts/smoke_dashboard.py`

**Interfaces:**
- Consumes: `setup.merge_env/tg_get_me/or_key_info/or_model_ids/SetupError/EXAMPLE_PATH` (Task 1), `web.back` (Task 4), `app.state.env_path` and `app.state.request_stop` (Task 4).
- Produces: routes `GET /settings`, `POST /settings/live`, `POST /settings/config`, `POST /restart`.

- [ ] **Step 1: Failing test.** Add:

```python
def config_form(**over: str) -> dict[str, str]:
    base = {
        "OPENROUTER_MODEL": settings.openrouter_model,
        "EXTRACTOR_MODEL": settings.extractor_model,
        "WHISPER_MODEL": settings.whisper_model,
        "OWNER_FIRST_NAME": settings.owner_first_name,
        "HISTORY_TURNS": str(settings.history_turns),
        "OWNER_USER_ID": str(settings.owner_user_id),
        "TG_BOT_TOKEN": "",
        "OPENROUTER_API_KEY": "",
    }
    return {**base, **over}


async def check_settings(app) -> None:
    from dotenv import dotenv_values

    async with await authed(app) as c:
        r = await c.get("/settings")
        assert r.status_code == 200
        assert TOKEN not in r.text and KEY not in r.text and setup.mask(TOKEN) in r.text

        # Live group writes the same bot_state keys the /commands use.
        r = await c.post("/settings/live", data={
            "paused": "on", "voice": "off", "quiet_start": "23:00", "quiet_end": "08:00",
            "delay_override": "45", "away_delay_override": "", "cooldown_override": "",
        })
        assert r.status_code == 303 and "err" not in r.headers["location"]
        assert await db.get_state_bool("paused") and not await db.get_state_bool("approval_mode")
        assert not await db.get_state_bool("innercircle_gate", default=True)
        assert await db.get_state("delay_override") == "45" and await db.get_state("quiet_end") == "08:00"
        assert await db.get_state("voice_override") == "off"
        r = await c.post("/settings/live", data={"delay_override": "abc"})
        assert "err=" in r.headers["location"] and await db.get_state("delay_override") == "45"
        r = await c.post("/settings/live", data={"quiet_start": "23:00"})
        assert "err=" in r.headers["location"]

        # Config: a live field is written to .env and applied in memory without a restart.
        r = await c.post("/settings/config", data=config_form(OPENROUTER_MODEL="a/b"))
        assert r.status_code == 303 and "restart" not in r.headers["location"]
        assert dotenv_values(ENV)["OPENROUTER_MODEL"] == "a/b" and settings.openrouter_model == "a/b"
        r = await c.post("/settings/config", data=config_form(OPENROUTER_MODEL="nope/x"))
        assert "err=" in r.headers["location"] and settings.openrouter_model == "a/b"

        # Review focus 3: network down while checking a new token -> error, .env untouched.
        new_token = "654321:NEWtokenNEWtokenNEWtokenNEWtoken1"
        real = setup.tg_get_me

        def down(token):
            raise httpx.ConnectError("down")

        setup.tg_get_me = down
        r = await c.post("/settings/config", data=config_form(TG_BOT_TOKEN=new_token))
        setup.tg_get_me = real
        assert "err=" in r.headers["location"] and dotenv_values(ENV).get("TG_BOT_TOKEN") != new_token

        # A validated token change is saved and asks for a restart.
        r = await c.post("/settings/config", data=config_form(TG_BOT_TOKEN=new_token))
        assert "restart=1" in r.headers["location"] and dotenv_values(ENV)["TG_BOT_TOKEN"] == new_token
        r = await c.post("/restart")
        assert r.status_code == 200 and STOPS == [1]
```

and call `await check_settings(app)` in `main()` after `check_status(app)`.

Run: `uv run python scripts/smoke_dashboard.py`
Expected: FAIL: `AssertionError` (GET `/settings` returns 404).

- [ ] **Step 2: Implement.** Add to `pages.py` imports `import re` and `import httpx`, and change `from .web import render` to `from .web import back, render`. Then append:

```python
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


@router.get("/settings")
async def settings_page(request: Request):
    return render(
        request, "settings.html",
        paused=await db.get_state_bool("paused"),
        approval=await db.get_state_bool("approval_mode"),
        innercircle=await db.get_state_bool("innercircle_gate", default=True),
        voice=await db.get_state("voice_override") or "",
        quiet_start=await db.get_state("quiet_start") or "",
        quiet_end=await db.get_state("quiet_end") or "",
        live_ints=[(key, label, getattr(settings, attr), await db.get_state(key) or "")
                   for key, (label, attr) in LIVE_INT_KEYS.items()],
        s=settings,
        models=sorted(await _model_ids() or []),
        restart=request.query_params.get("restart") == "1",
    )


@router.post("/settings/live")
async def save_live(request: Request):
    form = await request.form()
    quiet = (str(form.get("quiet_start", "")), str(form.get("quiet_end", "")))
    if any(quiet) and not all(_TIME_RE.match(t) for t in quiet):
        return back("/settings", err="Quiet hours need both a start and an end time.")
    ints = {key: str(form.get(key, "")).strip() for key in LIVE_INT_KEYS}
    for key, raw in ints.items():
        if raw and not raw.isdigit():
            return back("/settings", err=f"{LIVE_INT_KEYS[key][0]} must be a whole number (blank = default).")
    voice = str(form.get("voice", ""))
    if voice not in ("", "on", "off"):
        return back("/settings", err="Unknown voice setting.")
    await db.set_state_bool("paused", form.get("paused") == "on")
    await db.set_state_bool("approval_mode", form.get("approval_mode") == "on")
    await db.set_state_bool("innercircle_gate", form.get("innercircle_gate") == "on")
    await db.set_state("voice_override", voice)
    await db.set_state("quiet_start", quiet[0])
    await db.set_state("quiet_end", quiet[1])
    for key, raw in ints.items():
        await db.set_state(key, raw)  # "" = env default, same as /delay off
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
    changes: dict[str, str] = {}
    for key, old in current.items():
        new = str(form.get(key, "")).strip()
        if not new:
            return back("/settings", err=f"{key} can't be empty.")
        if new != old:
            changes[key] = new
    turns = changes.get("HISTORY_TURNS")
    if turns is not None and not (turns.isdigit() and 1 <= int(turns) <= 100):
        return back("/settings", err="History turns must be between 1 and 100.")
    if not changes.get("OWNER_USER_ID", "1").isdigit():
        return back("/settings", err="Owner user id must be a number.")
    ids = await _model_ids()
    for key in CHECKED_MODEL_KEYS:
        if key in changes and ids is not None and changes[key] not in ids:
            return back("/settings", err=f"Unknown model id: {changes[key]}")
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
        return back("/settings", err=f"Couldn't reach the service to check it ({type(e).__name__}); nothing saved.")
    if not changes:
        return back("/settings", msg="Nothing changed.")
    try:
        setup.merge_env(request.app.state.env_path, setup.EXAMPLE_PATH, changes)
    except OSError as e:
        return back("/settings", err=f"Couldn't write .env ({e}); nothing applied.")
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
```

`secretary/dashboard/templates/settings.html`:

```html
{% extends "base.html" %}
{% block content %}
<h1>Settings</h1>
{% if restart %}
<form method="post" action="/restart" class="card warn">
  <p>The token, API key or owner change needs a restart. Under pm2 the bot is back in a few seconds; without pm2 it stays stopped.</p>
  <button>Restart now</button>
</form>
{% endif %}

<form method="post" action="/settings/live" class="card">
  <h2>Live <small>applies to the next message, same as the /commands</small></h2>
  <label><input type="checkbox" name="paused" {{ "checked" if paused }}> Pause all auto-replies</label>
  <label><input type="checkbox" name="approval_mode" {{ "checked" if approval }}> Approval mode: every reply waits for you in Drafts</label>
  <label><input type="checkbox" name="innercircle_gate" {{ "checked" if innercircle }}> Inner-circle gate: hold emotional, long or after-silence messages from gf, family, bff and close friends</label>
  <label>Voice transcription
    <select name="voice">
      <option value="" {{ "selected" if voice == "" }}>Default ({{ "on" if s.voice_transcribe else "off" }})</option>
      <option value="on" {{ "selected" if voice == "on" }}>On</option>
      <option value="off" {{ "selected" if voice == "off" }}>Off</option>
    </select></label>
  <label>Quiet hours <input type="time" name="quiet_start" value="{{ quiet_start }}"> to
    <input type="time" name="quiet_end" value="{{ quiet_end }}"> <small>both empty = none</small></label>
  {% for key, label, default, value in live_ints %}
  <label>{{ label }} <input type="number" min="0" name="{{ key }}" value="{{ value }}" placeholder="{{ default }} (default)"></label>
  {% endfor %}
  <button>Save</button>
</form>

<form method="post" action="/settings/config" class="card">
  <h2>Config <small>saved to .env (the old one is backed up)</small></h2>
  <datalist id="models">{% for m in models %}<option value="{{ m }}">{% endfor %}</datalist>
  <label>Reply model <input name="OPENROUTER_MODEL" value="{{ s.openrouter_model }}" list="models" required></label>
  <label>Extractor model <input name="EXTRACTOR_MODEL" value="{{ s.extractor_model }}" list="models" required></label>
  <label>Voice model <input name="WHISPER_MODEL" value="{{ s.whisper_model }}" required></label>
  <label>Your first name <input name="OWNER_FIRST_NAME" value="{{ s.owner_first_name }}" dir="auto" required></label>
  <label>History turns per reply <input type="number" min="1" max="100" name="HISTORY_TURNS" value="{{ s.history_turns }}" required></label>
  <h3>Needs a restart</h3>
  <label>Telegram bot token <input type="password" name="TG_BOT_TOKEN" placeholder="{{ s.tg_bot_token|mask }} (blank keeps it)" autocomplete="off"></label>
  <label>OpenRouter API key <input type="password" name="OPENROUTER_API_KEY" placeholder="{{ s.openrouter_api_key|mask }} (blank keeps it)" autocomplete="off"></label>
  <label>Owner user id <input name="OWNER_USER_ID" value="{{ s.owner_user_id }}" inputmode="numeric" required></label>
  <button>Save</button>
</form>
{% endblock %}
```

`secretary/dashboard/templates/restarting.html`:

```html
{% extends "base.html" %}
{% block content %}
<meta http-equiv="refresh" content="8;url=/">
<h1>Restarting…</h1>
<p>Under pm2 the bot comes back in a few seconds and this page reloads. Without pm2 the bot is now stopped; start it again by hand.</p>
{% endblock %}
```

- [ ] **Step 3: Run checks**

Run: `uv run python scripts/smoke_dashboard.py`
Expected: `smoke_dashboard OK`

- [ ] **Step 4: Commit** (only if asked): `secretary/dashboard/ scripts/smoke_dashboard.py`

---

## Phase 3: Contacts, prompts, memory

### Task 8: Contacts list + detail + memory

**Files:**
- Create: `secretary/dashboard/contacts.py`
- Create: `secretary/dashboard/templates/contacts.html`, `secretary/dashboard/templates/contact.html`
- Modify: `secretary/dashboard/app.py` (include router)
- Modify: `scripts/smoke_dashboard.py`

**Interfaces:**
- Consumes: `db.list_chats/get_override/upsert_override/list_memory/add_memory/expire_memory/enqueue/load_history` (existing + Task 3); `commands._active_conn_id`, `commands.VALID_RELATIONSHIPS`; `prompts.clear_cache`.
- Produces (Task 9 uses these):
  - `contacts.router`
  - `contacts.write_prompt(path: Path, text: str) -> None`
  - `contacts.read_text(path: Path) -> str | None`
  - `contacts.display_name(row: dict) -> str`

- [ ] **Step 1: Failing test.** Add:

```python
async def check_contacts(app) -> None:
    async with await authed(app) as c:
        r = await c.get("/contacts")
        assert r.status_code == 200 and "/contacts/5" in r.text
        assert "/contacts/5" not in (await c.get("/contacts", params={"q": "zzz"})).text
        assert (await c.get("/contacts/5")).status_code == 200

        r = await c.post("/contacts/5/profile", data={
            "relationship": "bff", "nickname": "Sami", "persona_extra": "likes chess", "paused": "on"})
        assert r.status_code == 303 and "err" not in r.headers["location"]
        o = await db.get_override(conn_id="c1", chat_id=5)
        assert (o["relationship"], o["nickname"], o["persona_extra"], o["paused"]) == ("bff", "Sami", "likes chess", 1)
        r = await c.post("/contacts/5/profile", data={"relationship": "boss"})
        assert "err=" in r.headers["location"]

        # Review focus 2: Persian + CRLF from a textarea lands as UTF-8 with LF only.
        persian = "سلام\r\nخوبی؟"
        await c.post("/contacts/5/prompt", data={"text": persian})
        f = settings.prompts_dir / "contacts" / "5.txt"
        assert f.read_bytes() == "سلام\nخوبی؟\n".encode("utf-8")
        await c.post("/contacts/5/prompt", data={"text": "  "})
        assert not f.exists()

        await c.post("/contacts/5/memory", data={"kind": "fact", "content": "has a cat"})
        mems = await db.list_memory(conn_id="c1", chat_id=5)
        assert [m["content"] for m in mems] == ["has a cat"]
        await c.post(f"/contacts/5/memory/{mems[0]['id']}/expire")
        assert await db.list_memory(conn_id="c1", chat_id=5) == []
        r = await c.post("/contacts/5/extract")
        assert r.status_code == 303 and await db.claim_next() is not None
        assert (await c.get("/contacts/abc")).status_code == 422
```

and call `await check_contacts(app)` in `main()` after `check_settings(app)`.

Run: `uv run python scripts/smoke_dashboard.py`
Expected: FAIL: `AssertionError` (GET `/contacts` returns 404).

- [ ] **Step 2: Implement** `secretary/dashboard/contacts.py`:

```python
"""Contacts (tags, notes, prompt files, memory) and the Prompts editor."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Form, Request

from .. import commands, db, prompts
from ..config import settings
from .web import back, render

router = APIRouter()

MEMORY_KINDS = ("fact", "preference", "event", "promise", "inside_joke", "open_thread")
NO_CONNECTION = "No active business connection yet. Connect the bot in Telegram first."


def display_name(row: dict[str, Any]) -> str:
    full = " ".join(p for p in (row.get("tg_first_name"), row.get("tg_last_name")) if p)
    username = f"@{row['tg_username']}" if row.get("tg_username") else ""
    return row.get("nickname") or full or username or str(row["chat_id"])


def read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def write_prompt(path: Path, text: str) -> None:
    """Save textarea content as UTF-8 with LF endings; empty text deletes the file."""
    text = text.replace("\r\n", "\n").strip()
    if text:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text + "\n", encoding="utf-8", newline="\n")
    else:
        path.unlink(missing_ok=True)
    prompts.clear_cache()


def _contact_file(chat_id: int) -> Path:
    return settings.prompts_dir / "contacts" / f"{chat_id}.txt"


@router.get("/contacts")
async def contacts(request: Request, q: str = ""):
    rows = await db.list_chats()
    for r in rows:
        r["name"] = display_name(r)
    needle = q.strip().lower()
    if needle:
        rows = [r for r in rows
                if needle in f"{r['chat_id']} {r['name']} {r.get('tg_username') or ''}".lower()]
    return render(request, "contacts.html", rows=rows, q=q)


@router.get("/contacts/{chat_id}")
async def contact(request: Request, chat_id: int):
    conn_id = await commands._active_conn_id()
    if conn_id is None:
        return back("/contacts", err=NO_CONNECTION)
    override = await db.get_override(conn_id=conn_id, chat_id=chat_id) or {}
    return render(
        request, "contact.html",
        chat_id=chat_id,
        name=display_name({**override, "chat_id": chat_id}),
        o=override,
        relationships=sorted(commands.VALID_RELATIONSHIPS),
        prompt_text=read_text(_contact_file(chat_id)) or "",
        memories=await db.list_memory(conn_id=conn_id, chat_id=chat_id),
        kinds=MEMORY_KINDS,
        history=await db.load_history(conn_id=conn_id, chat_id=chat_id, limit=20),
    )


@router.post("/contacts/{chat_id}/profile")
async def save_profile(
    chat_id: int,
    relationship: str = Form(""),
    nickname: str = Form(""),
    persona_extra: str = Form(""),
    paused: str = Form(""),
):
    conn_id = await commands._active_conn_id()
    if conn_id is None:
        return back("/contacts", err=NO_CONNECTION)
    if relationship not in commands.VALID_RELATIONSHIPS:
        return back(f"/contacts/{chat_id}", err="Unknown relationship.")
    await db.upsert_override(
        conn_id=conn_id, chat_id=chat_id,
        relationship=relationship,
        nickname=nickname.strip(),
        persona_extra=persona_extra.replace("\r\n", "\n").strip(),
        paused=paused == "on",
    )
    prompts.clear_cache()
    return back(f"/contacts/{chat_id}", msg="Saved.")


@router.post("/contacts/{chat_id}/prompt")
async def save_contact_prompt(chat_id: int, text: str = Form("")):
    write_prompt(_contact_file(chat_id), text)
    return back(f"/contacts/{chat_id}", msg="Prompt file saved.")


@router.post("/contacts/{chat_id}/memory")
async def add_memory(chat_id: int, kind: str = Form(""), content: str = Form("")):
    conn_id = await commands._active_conn_id()
    if conn_id is None:
        return back("/contacts", err=NO_CONNECTION)
    if kind not in MEMORY_KINDS or not content.strip():
        return back(f"/contacts/{chat_id}", err="Pick a kind and write the memory.")
    await db.add_memory(conn_id=conn_id, chat_id=chat_id, kind=kind, content=content.strip())
    return back(f"/contacts/{chat_id}", msg="Memory added.")


@router.post("/contacts/{chat_id}/memory/{memory_id}/expire")
async def expire_memory(chat_id: int, memory_id: int):
    await db.expire_memory(memory_id)
    return back(f"/contacts/{chat_id}", msg="Memory removed.")


@router.post("/contacts/{chat_id}/extract")
async def extract(chat_id: int):
    conn_id = await commands._active_conn_id()
    if conn_id is None:
        return back("/contacts", err=NO_CONNECTION)
    await db.enqueue(conn_id=conn_id, chat_id=chat_id)
    return back(f"/contacts/{chat_id}",
                msg="Extraction queued. The memory worker runs it within ~15s (it skips chats with few new messages).")
```

`secretary/dashboard/templates/contacts.html`:

```html
{% extends "base.html" %}
{% block content %}
<h1>Contacts</h1>
<form method="get" action="/contacts" class="row">
  <input name="q" value="{{ q }}" placeholder="name, @username or chat id" dir="auto">
  <button>Search</button>
</form>
<table>
  <tr><th>Name</th><th>Relationship</th><th>Last message</th><th>Messages</th><th></th></tr>
  {% for r in rows %}
  <tr>
    <td dir="auto"><a href="/contacts/{{ r.chat_id }}">{{ r.name }}</a>{% if r.tg_username %} <small>@{{ r.tg_username }}</small>{% endif %}</td>
    <td>{{ r.relationship or "untagged" }}</td>
    <td>{{ r.last_seen|ts }}</td>
    <td>{{ r.msg_count }}</td>
    <td>{{ "paused" if r.paused }}</td>
  </tr>
  {% else %}<tr><td colspan="5">No chats yet.</td></tr>{% endfor %}
</table>
{% endblock %}
```

`secretary/dashboard/templates/contact.html`:

```html
{% extends "base.html" %}
{% block content %}
<p><a href="/contacts">← Contacts</a></p>
<h1 dir="auto">{{ name }} <small>{{ chat_id }}</small></h1>

<form method="post" action="/contacts/{{ chat_id }}/profile" class="card">
  <h2>Profile</h2>
  <label>Relationship
    <select name="relationship">
      {% for r in relationships %}<option value="{{ r }}" {{ "selected" if (o.relationship or "unknown") == r }}>{{ r }}</option>{% endfor %}
    </select></label>
  <label>Nickname <input name="nickname" value="{{ o.nickname or '' }}" dir="auto"></label>
  <label><input type="checkbox" name="paused" {{ "checked" if o.paused }}> Pause auto-replies in this chat</label>
  <label>Note (added to the prompt for this chat)
    <textarea name="persona_extra" rows="4" dir="auto">{{ o.persona_extra or '' }}</textarea></label>
  <button>Save</button>
</form>

<form method="post" action="/contacts/{{ chat_id }}/prompt" class="card">
  <h2>Prompt file <small>prompts/contacts/{{ chat_id }}.txt</small></h2>
  <textarea name="text" rows="10" dir="auto">{{ prompt_text }}</textarea>
  <button>Save</button> <small>Empty deletes the file.</small>
</form>

<section class="card">
  <h2>Memory</h2>
  <table>
    {% for m in memories %}
    <tr><td>{{ m.kind }}</td><td dir="auto">{{ m.content }}</td><td>{{ m.created_at|ts }}</td>
      <td><form method="post" action="/contacts/{{ chat_id }}/memory/{{ m.id }}/expire"><button>Remove</button></form></td></tr>
    {% else %}<tr><td>No memories yet.</td></tr>{% endfor %}
  </table>
  <form method="post" action="/contacts/{{ chat_id }}/memory" class="row">
    <select name="kind">{% for k in kinds %}<option>{{ k }}</option>{% endfor %}</select>
    <input name="content" placeholder="new memory" dir="auto" required>
    <button>Add</button>
  </form>
  <form method="post" action="/contacts/{{ chat_id }}/extract"><button>Extract from recent messages now</button></form>
</section>

{% if o.style_fingerprint %}
<section class="card"><h2>Style fingerprint</h2><pre dir="auto">{{ o.style_fingerprint }}</pre></section>
{% endif %}

<section class="card">
  <h2>Last messages</h2>
  {% for m in history %}
  <p dir="auto"><b>{{ "them" if m.role == "user" else "you" }}:</b> {{ m.content }}</p>
  {% else %}<p>No messages.</p>{% endfor %}
</section>
{% endblock %}
```

In `app.py`, change the import to `from . import auth, contacts, pages` and add `app.include_router(contacts.router)` after the `pages` include.

- [ ] **Step 3: Run checks**

Run: `PYTHONIOENCODING=utf-8 uv run python scripts/smoke_dashboard.py`
Expected: `smoke_dashboard OK`

- [ ] **Step 4: Commit** (only if asked): `secretary/dashboard/ scripts/smoke_dashboard.py`

### Task 9: Prompts editor

**Files:**
- Modify: `secretary/dashboard/contacts.py` (append)
- Create: `secretary/dashboard/templates/prompts.html`
- Modify: `scripts/smoke_dashboard.py`

**Interfaces:**
- Consumes: `contacts.write_prompt`, `contacts.read_text` (Task 8); `prompts.PERSONAS`; `commands.VALID_RELATIONSHIPS`.
- Produces: routes `GET /prompts`, `POST /prompts/{name}`.

- [ ] **Step 1: Failing test.** Add:

```python
async def check_prompts(app) -> None:
    personas = settings.prompts_dir / "personas"
    personas.mkdir(parents=True, exist_ok=True)
    (personas / "friend.example.txt").write_text("EXAMPLE FRIEND TEXT", encoding="utf-8")
    async with await authed(app) as c:
        r = await c.get("/prompts")
        assert r.status_code == 200 and "EXAMPLE FRIEND TEXT" in r.text and "about_me" in r.text
        await c.post("/prompts/gf", data={"text": "عزیزم\r\nhi"})
        assert (personas / "gf.txt").read_bytes() == "عزیزم\nhi\n".encode("utf-8")
        await c.post("/prompts/about_me", data={"text": "I live in Tehran"})
        assert (settings.prompts_dir / "about_me.txt").read_text(encoding="utf-8") == "I live in Tehran\n"
        assert (await c.post("/prompts/nope", data={"text": "x"})).status_code == 404
        assert (await c.post("/prompts/..%5Cx", data={"text": "x"})).status_code == 404
        assert not (settings.prompts_dir / "nope.txt").exists()
        assert (personas / "friend.example.txt").read_text(encoding="utf-8") == "EXAMPLE FRIEND TEXT"
```

and call `await check_prompts(app)` in `main()` after `check_contacts(app)`.

Run: `PYTHONIOENCODING=utf-8 uv run python scripts/smoke_dashboard.py`
Expected: FAIL: `AssertionError` (GET `/prompts` returns 404).

- [ ] **Step 2: Implement.** Add `from fastapi import HTTPException` to the fastapi import in `contacts.py`, then append:

```python
PROMPT_NAMES = ("about_me", *sorted(commands.VALID_RELATIONSHIPS))


def _prompt_paths(name: str) -> tuple[Path, Path]:
    """(your file, shipped example) for a whitelisted prompt name."""
    folder = settings.prompts_dir if name == "about_me" else settings.prompts_dir / "personas"
    return folder / f"{name}.txt", folder / f"{name}.example.txt"


@router.get("/prompts")
async def prompts_page(request: Request):
    items = []
    for name in PROMPT_NAMES:
        real, example = _prompt_paths(name)
        text, source = read_text(real), "your file"
        if text is None:
            text, source = read_text(example), "example — saving creates your own file"
        if text is None and name in prompts.PERSONAS:
            text, source = prompts.PERSONAS[name], "built-in default — saving creates your own file"
        items.append({"name": name, "text": text or "", "source": source if text else "empty"})
    return render(request, "prompts.html", items=items)


@router.post("/prompts/{name}")
async def save_prompt(name: str, text: str = Form("")):
    if name not in PROMPT_NAMES:  # whitelist: user input never forms a path
        raise HTTPException(status_code=404)
    write_prompt(_prompt_paths(name)[0], text)
    return back("/prompts", msg=f"Saved {name}.")
```

`secretary/dashboard/templates/prompts.html`:

```html
{% extends "base.html" %}
{% block content %}
<h1>Prompts</h1>
<p>About me goes into every reply. Each persona applies to contacts tagged with that relationship. Write <code>{owner_first_name}</code> where your name goes.</p>
{% for p in items %}
<details class="card" {{ "open" if loop.first }}>
  <summary><strong>{{ p.name }}</strong> <small>{{ p.source }}</small></summary>
  <form method="post" action="/prompts/{{ p.name }}">
    <textarea name="text" rows="14" dir="auto">{{ p.text }}</textarea>
    <button>Save</button> <small>Empty deletes your file, so the example applies again.</small>
  </form>
</details>
{% endfor %}
{% endblock %}
```

- [ ] **Step 3: Run checks**

Run: `PYTHONIOENCODING=utf-8 uv run python scripts/smoke_dashboard.py`
Expected: `smoke_dashboard OK`

- [ ] **Step 4: Commit** (only if asked): `secretary/dashboard/ scripts/smoke_dashboard.py`

---

## Phase 4: Drafts

### Task 10: Drafts page + `_resolve_pending(bot, ...)`

**Files:**
- Modify: `secretary/commands.py:652-739` (`_resolve_pending` + its 4 callers)
- Create: `secretary/dashboard/drafts.py`, `secretary/dashboard/templates/drafts.html`
- Modify: `secretary/dashboard/app.py` (include router)
- Modify: `scripts/smoke_dashboard.py`

**Interfaces:**
- Consumes: `db.list_open_pending/get_pending` (Task 3); `web.back/render`.
- Produces:
  - `commands._resolve_pending(bot: Any, pid: int, status: str, text: str | None = None) -> str`
  - `drafts.router`

- [ ] **Step 1: Failing test.** Add:

```python
async def check_drafts(app) -> None:
    sent = await db.create_pending(conn_id="c1", chat_id=5, contact_name="Sam", contact_msg="hi", draft="hey there")
    edited = await db.create_pending(conn_id="c1", chat_id=5, contact_name="Sam", contact_msg="hi", draft="draft")
    skipped = await db.create_pending(conn_id="c1", chat_id=5, contact_name="Sam", contact_msg="hi", draft="nah")
    async with await authed(app) as c:
        r = await c.get("/drafts")
        assert r.status_code == 200 and "hey there" in r.text
        r = await c.post(f"/drafts/{sent}", data={"action": "send", "text": "hey there"})
        assert "msg=" in r.headers["location"] and BOT.sent[-1] == (5, "hey there", "c1")
        assert (await db.get_pending(sent))["status"] == "approved"
        r = await c.post(f"/drafts/{sent}", data={"action": "send", "text": "hey there"})
        assert "err=" in r.headers["location"] and len(BOT.sent) == 1  # double submit can't resend
        await c.post(f"/drafts/{edited}", data={"action": "send", "text": "my own words\r\n"})
        assert BOT.sent[-1] == (5, "my own words", "c1") and (await db.get_pending(edited))["status"] == "edited"
        await c.post(f"/drafts/{skipped}", data={"action": "skip"})
        assert (await db.get_pending(skipped))["status"] == "skipped" and len(BOT.sent) == 2
```

and call `await check_drafts(app)` in `main()` after `check_prompts(app)`. Note: `check_db` (Task 3) already left one open draft (`"yo"`); it doesn't affect these asserts.

Run: `PYTHONIOENCODING=utf-8 uv run python scripts/smoke_dashboard.py`
Expected: FAIL: `AssertionError` (GET `/drafts` returns 404).

- [ ] **Step 2: Refactor `_resolve_pending`.** In `secretary/commands.py`:
  - Change the signature to `async def _resolve_pending(bot: Any, pid: int, status: str, text: str | None = None) -> str:`.
  - Change its send call from `await ctx.bot.send_message(` to `await bot.send_message(`.
  - In the four callers (`on_approve`, `on_hitl_callback`, `on_edit`, `on_skip`), change `_resolve_pending(ctx, ` to `_resolve_pending(ctx.bot, `.

Run: `grep -n "_resolve_pending(" secretary/commands.py`
Expected: the def line plus exactly 4 calls, all passing `ctx.bot`.

- [ ] **Step 3: Implement** `secretary/dashboard/drafts.py`:

```python
"""HITL drafts: send, edit-and-send, or skip pending replies from the browser."""
from __future__ import annotations

from fastapi import APIRouter, Form, Request

from .. import commands, db
from .web import back, render

router = APIRouter()


@router.get("/drafts")
async def drafts(request: Request):
    return render(request, "drafts.html", drafts=await db.list_open_pending())


@router.post("/drafts/{pid}")
async def resolve(request: Request, pid: int, action: str = Form(""), text: str = Form("")):
    bot = request.app.state.bot
    if action == "skip":
        result = await commands._resolve_pending(bot, pid, "skipped")
    elif action == "send":
        pending = await db.get_pending(pid)
        if pending is None:
            return back("/drafts", err=f"pending #{pid} not found")
        body = text.replace("\r\n", "\n").strip()
        if not body:
            return back("/drafts", err="The reply is empty.")
        edited = body != pending["draft"].strip()
        # claim_pending inside _resolve_pending makes a double submit a no-op.
        result = await commands._resolve_pending(
            bot, pid, "edited" if edited else "approved", body if edited else None)
    else:
        return back("/drafts", err="Unknown action.")
    if result.startswith(("sent", "skipped")):
        return back("/drafts", msg=result)
    return back("/drafts", err=result)
```

`secretary/dashboard/templates/drafts.html`:

```html
{% extends "base.html" %}
{% block content %}
<h1>Drafts</h1>
{% for d in drafts %}
<form method="post" action="/drafts/{{ d.id }}" class="card">
  <p><a href="/contacts/{{ d.chat_id }}" dir="auto">{{ d.contact_name or d.chat_id }}</a>
     <small>#{{ d.id }} · expires {{ d.expires_at|ts }}</small></p>
  <blockquote dir="auto">{{ d.contact_msg }}</blockquote>
  <textarea name="text" rows="3" dir="auto">{{ d.draft }}</textarea>
  <button name="action" value="send">Send</button>
  <button name="action" value="skip">Skip</button>
  <small>Edit the text before sending if you like.</small>
</form>
{% else %}
<p>No drafts waiting. Approval mode and the inner-circle gate put replies here.</p>
{% endfor %}
{% endblock %}
```

In `app.py`, change the import to `from . import auth, contacts, drafts, pages` and add `app.include_router(drafts.router)` after the `contacts` include.

- [ ] **Step 4: Run checks**

Run: `PYTHONIOENCODING=utf-8 uv run python scripts/smoke_dashboard.py && uv run python scripts/smoke_core.py`
Expected: `smoke_dashboard OK`, then `smoke_core OK`

- [ ] **Step 5: Commit** (only if asked): `secretary/commands.py secretary/dashboard/ scripts/smoke_dashboard.py`

---

### Task 11: Docs + full verification

**Files:**
- Modify: `CLAUDE.md` (Layout tree, Owner control surface, Smoke tests, Working notes)
- Modify: `README.md` (add a short "Dashboard" section after Setup)
- Modify: `docs/superpowers/specs/2026-10-04-setup-wizard-dashboard-design.md` §4 Files (record the final file split)

- [ ] **Step 1: CLAUDE.md**:
  - Layout tree: add `setup.py  first-run wizard (validates token/key, owner /start code, writes .env)` and `dashboard/  owner web UI served in-process on 127.0.0.1 (auth, pages, contacts, drafts, templates/, static/)`.
  - Owner control surface: add `/dashboard` (one-time login link) and a line saying the dashboard mirrors the same `bot_state` keys.
  - Smoke tests: add `uv run python scripts/smoke_setup.py` and `PYTHONIOENCODING=utf-8 uv run python scripts/smoke_dashboard.py`.
  - Working notes (Internal patterns), add three bullets:
    - Dashboard runs in the bot's asyncio loop. `_Server.capture_signals` is a no-op, so uvicorn doesn't steal SIGINT/SIGTERM. A busy port logs an error and the bot keeps running.
    - Login works only through one-time links (`/dashboard`, or the end of `secretary.setup`). Tokens and sessions are sha256-hashed in `bot_state` under the `dash_login:` / `dash_session:` prefixes.
    - Guards: loopback `Host` (any port) plus `Origin` on POST. Never add a public-bind option; access is through `ssh -L`.
  - Also note: `secretary/setup.py` must not import `secretary.config` at module level.

- [ ] **Step 2: README** — add after Setup:

```markdown
## Dashboard

The bot serves a web dashboard on `127.0.0.1:8780` (set `DASHBOARD_PORT` to change it, `DASHBOARD_ENABLED=false` to turn it off). Send `/dashboard` to your bot to get a one-time login link. When the bot runs on a server, open a tunnel from your computer first: `ssh -L 8780:127.0.0.1:8780 user@server`. The dashboard covers live settings, config, business-connection status, contacts (relationship, notes, prompt files, memory), persona prompts and the approval queue. It is never exposed publicly.
```

- [ ] **Step 3: Spec sync.** The spec was already updated for the file split, the `list_chats` search, Extract-now via `db.enqueue`, and Drafts Send/Skip. Diff the shipped code against spec §3–§4 and fix any remaining mismatch in the spec.

- [ ] **Step 4: Automated gate.** Run each command and confirm its output:

```bash
uv run python scripts/smoke_setup.py
PYTHONIOENCODING=utf-8 uv run python scripts/smoke_dashboard.py
uv run python scripts/smoke_core.py
uv run python -c "import secretary.__main__; print('OK')"
bash -n scripts/setup-ubuntu.sh
git diff --stat main
```

Expected: `smoke_setup OK`, `smoke_dashboard OK`, `smoke_core OK`, `OK`, no bash output. The diff stat must show no deleted tests and no unrelated files. Check line endings: `git diff main --stat` should not list whole-file rewrites of untouched files.

- [ ] **Step 5: Real-page check (Chrome DevTools MCP).** Do not start the real bot (see Global Constraints). Instead, write a throwaway harness in the scratchpad, not the repo:
  - it copies `secretary.db` to the scratchpad;
  - it sets env `DB_PATH` to the copy, plus dummy `TG_BOT_TOKEN` / `OPENROUTER_API_KEY` and the real `OWNER_USER_ID` from `.env`;
  - it builds `create_app(FakeBot(), lambda: None, env_path=<scratch .env>)`;
  - it mints `auth.create_login_token()`, prints the link, and runs `make_server(app, 8780)` via `asyncio.run(run_server(...))`.

  Start it in the background. Open the link with Chrome DevTools MCP, then:
  - click through Status, Settings, Contacts, one contact, Prompts and Drafts;
  - check the console for errors;
  - check Persian text renders right-to-left in the contact page;
  - check the page at 400px width.

  Stop the harness and delete the scratch DB copy afterwards.

- [ ] **Step 6: Finish.** Run `superpowers:verification-before-completion`, then `/code-review` and `/ponytail-review` per the owner's rules. Report what's still running (none should be).
