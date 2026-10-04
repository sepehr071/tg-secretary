# Setup wizard + web dashboard — design

Date: 2026-10-04
Status: approved in brainstorming, pending spec review

## Goal

Make tg-secretary easy to install and run for people who clone the public repo onto a headless Ubuntu VPS:

1. A terminal setup wizard that collects and **validates** the Telegram token, OpenRouter key and owner identity, then writes `.env`.
2. A web dashboard, served by the bot process on `127.0.0.1`, reached through an SSH tunnel, that covers bot settings, status, contacts and prompts, memory, and the HITL draft queue.

Telegram `/commands` keep working unchanged. The dashboard is an additional surface over the same data.

## Decisions (from brainstorming)

| Topic | Decision |
|---|---|
| Audience | Other people, bot on a headless VPS |
| Dashboard scope (v1) | Bot settings, contacts and prompts, status and stats, memory and HITL drafts |
| Remote access | SSH tunnel only. The dashboard binds `127.0.0.1`; there is no public-bind option |
| Setup location | Terminal wizard (`uv run python -m secretary.setup`) |
| Process model | Dashboard runs inside the bot process (same asyncio loop, one pm2 app) |
| Frontend | Server-rendered Jinja, plain HTML forms, minimal vanilla JS. No SPA, no build step, no new dependencies (FastAPI, Jinja2, uvicorn, httpx, python-dotenv are already in `pyproject.toml`) |

Prior art reviewed: OpenClaw (`openclaw onboard`, Control UI with loopback bind + one-time link + `ssh -L` hint on headless hosts, pairing codes for owner identity) and Hermes Agent (`hermes setup`, re-run shows current values and backs up config, schema-driven config form, redacted keys). Neither validates the Telegram token with `getMe` during setup; this design does.

## 1. Setup wizard

**Entry point:** `uv run python -m secretary.setup` (new module `secretary/setup.py`). It is a separate module because `secretary/config.py` instantiates `Settings()` at import and fails when `.env` is missing. The wizard must not import `secretary.config` until after `.env` is written.

`scripts/setup-ubuntu.sh` step 6 (the interactive `.env` block) is replaced by a call to this wizard, preceded by `pm2 stop tg-secretary 2>/dev/null || true`. Windows and macOS users run the same command directly.

**Re-run safety**
- If `.env` exists, each prompt shows the current value (secrets masked as `1234…abcd`); pressing Enter keeps it.
- Before writing, the existing `.env` is copied to `.env.<YYYYmmdd-HHMMSS>.bak` (already covered by the `*.bak` gitignore rule).
- Ctrl+C at any prompt aborts and writes nothing.
- Keys the wizard does not ask about are preserved as they are.

**Steps**

1. **Telegram bot token.** Hidden input (`getpass`). Validated with `GET https://api.telegram.org/bot<token>/getMe`.
   - Success prints `@username`.
   - 401 re-asks.
   - If `result.can_connect_to_business` is false, print the BotFather steps (`/mybots` → bot → Bot Settings → Business Mode → enable) and re-check on Enter.
2. **OpenRouter API key.** Hidden input. Validated with `GET https://openrouter.ai/api/v1/key` (free).
   - Success prints remaining credit (`limit_remaining`, or "no limit").
   - 401/403 re-asks.
3. **Owner proof.**
   - Generate a 6-character code with `secrets` from an unambiguous alphabet (no `0 O 1 I L`).
   - Print `https://t.me/<username>?start=<code>`.
   - Long-poll `getUpdates` (`timeout=25`, `allowed_updates=["message"]`) for up to 10 minutes, until a private-chat message with text `/start <code>` arrives. Capture `from.id` and `from.first_name`, then acknowledge the updates (`offset = last_update_id + 1`).
   - Send the owner a short confirmation DM ("Verified — you are the owner of this secretary.").
   - Ctrl+C during the wait falls back to manual entry of the numeric id.
   - HTTP 409 means another process is polling the bot. Print "the bot is running — stop it first: `pm2 stop tg-secretary`" and retry on Enter.
   - On re-run with an existing `OWNER_USER_ID`, ask "Keep owner <id>? [Y/n]" and skip the poll on yes.
   - Only someone who can see the terminal knows the code, so a stranger DMing the bot during setup cannot claim ownership.
4. **First name and reply model.**
   - First name defaults to the Telegram `first_name` captured in step 3.
   - The reply model defaults to the current value or `openai/gpt-5.5`, and is checked against the id list from `GET https://openrouter.ai/api/v1/models`. An unknown id re-asks. If the list cannot be fetched, the wizard warns and accepts the value.
   - All other tunables keep their current values or defaults; the dashboard edits them.

**Write and finish**
- Merge the values into `.env` with `python-dotenv` `set_key`. On a fresh install, start from `.env.example` so every documented key is present. `chmod 600` on POSIX.
- Import `secretary.config` and `secretary.db`, run `init_db()`, and create a one-time dashboard login token (see §2).
- Print:
  - Start command: `pm2 start ecosystem.config.cjs` (or `uv run python -m secretary` without pm2).
  - Connect steps: Telegram → Settings → Business → Chatbots → enter `@username`, grant "reply to messages" and "read messages".
  - Dashboard access. Over SSH (`$SSH_CONNECTION` set): `ssh -L 8780:127.0.0.1:8780 <$USER>@<server ip from $SSH_CONNECTION>`. Locally: none needed.
  - The one-time link `http://127.0.0.1:8780/login#t=<token>` (valid 1 hour), and "lost it? send /dashboard to your bot".

**Network errors.** A connection failure names the layer, e.g. "can't reach api.telegram.org — check HTTPS_PROXY / network". It offers `[r]etry` or `[s]kip this check`. httpx honours `HTTPS_PROXY` by default.

## 2. Dashboard runtime and security

**Config additions** (`secretary/config.py`, `.env.example`):
- `dashboard_enabled: bool = True`
- `dashboard_port: int = 8780`

There is no host setting. The bind address is hard-coded to `127.0.0.1`.

**Runtime.** In `secretary/__main__.py`, after the Telegram app starts:
- If `settings.dashboard_enabled`, build the app with `create_app(bot_app, request_stop)`.
- Run `uvicorn.Server(Config(app, host="127.0.0.1", port=settings.dashboard_port, access_log=False, log_level="warning"))` as an asyncio task.
- Disable uvicorn's own signal handling, because the bot already owns SIGINT/SIGTERM.
- On shutdown, set `server.should_exit = True` and await the task before closing the DB.
- If the port is taken, log an error and keep the bot running without the dashboard.

**Login: one-time links only (no password)**
- **Token creation.** A token is `secrets.token_urlsafe(32)`, valid 1 hour, stored as `bot_state` key `dash_login:<sha256 hex>` with the expiry epoch as value.
- **Link sources:**
  - the setup wizard;
  - the new owner-only bot command `/dashboard`. It replies with the `ssh -L` hint and the link, with link previews disabled.
- **Link format.** The link is `/login#t=<token>`. The token sits in the URL fragment, so it never reaches the server in a GET, any log, or a link prefetcher. The login page has a token field; a few lines of JS fill it from the fragment and submit. Without JS, the user pastes the token.
- **Token exchange.** `POST /login` consumes the token atomically: a new `db.pop_state(key)` helper using `DELETE … RETURNING value`. If the token is unexpired, the server creates a session.
- **Session storage.** A session is `secrets.token_urlsafe(32)`, stored as `dash_session:<sha256 hex>` with a 7-day expiry. Because sessions live in `bot_state`, they survive pm2 restarts.
- **Cookie.** `tgs_session`, `HttpOnly`, `SameSite=Strict`, `Path=/`, `Max-Age` 7 days. It is not `Secure`, because the dashboard is plain HTTP over the tunnel.
- **Cleanup.** Every login deletes expired `dash_*` rows.
- **Logout.** `POST /logout` deletes the session row and the cookie.

**Guards** (middleware, every request):
- **Host.** The hostname in `Host` must be `127.0.0.1`, `localhost` or `[::1]`, on any port, so `ssh -L 9000:…` works. Anything else gets 400. This blocks DNS rebinding.
- **Origin.** Every POST must carry `Origin` equal to `http://<Host header>`, else 403 (CSRF). This complements `SameSite=Strict`.
- **Session.** Every route except `/login` and `/static/*` requires a valid session. Without one, GET redirects to `/login`, which explains "send /dashboard to your bot", and POST returns 401.

**Secrets**
- Token and API key are never rendered. They show as `1234…abcd`.
- Their form fields are empty password inputs; leaving one blank keeps the current value.

## 3. Pages

All pages are server-rendered. Forms POST, then redirect to GET (post-redirect-get) and show a flash message. Persian content fields use `dir="auto"`. Nav: Status · Settings · Contacts · Prompts · Drafts (pending count) · Log out.

**Status `/`**
- Bot `@username`, uptime.
- Each business connection from the `connections` table: enabled state, plus missing rights (`can_reply`, `can_read_messages`).
- Current pause / approval / inner-circle state.
- Pending draft count.
- OpenRouter credit via `/api/v1/key`, fetched on page load. A failure shows "unavailable" and does not break the page.
- Reply stats and the latest bot replies. The stats SQL currently inline in `commands.on_stats` moves into a new `db.get_stats()`; `/stats` and the Status page both call it.

**Settings `/settings`**

*Live* group (`bot_state`, applies instantly). It uses exactly the keys the `/commands` use, so the two surfaces always agree:
- `paused`, `approval_mode`, `innercircle_gate`, `voice_override`: on / off / env default for voice.
- `quiet_start` / `quiet_end` via `<input type="time">`. Empty means off.
- `delay_override`, `away_delay_override`, `cooldown_override` (read by `handlers._live_int`). A non-negative integer; blank stores `""`, which resets to the env default, matching `/delay off`.

*Config* group (`.env`). The current `.env` is backed up first, then values are written with `set_key`.
- **Live-applied fields:** `OPENROUTER_MODEL`, `EXTRACTOR_MODEL`, `WHISPER_MODEL`, `OWNER_FIRST_NAME`, `HISTORY_TURNS`. Each is also assigned onto the in-memory `settings` object, because `llm`, `memory`, `voice` and `prompts` read these per call.
  - Model fields get a native `<datalist>` filled from `/api/v1/models`.
  - Unknown model ids are rejected. If the list cannot be fetched, the form warns and accepts the value.
- **Restart-required fields:** `TG_BOT_TOKEN` (validated with getMe), `OPENROUTER_API_KEY` (validated with `/api/v1/key`; the OpenAI client in `llm.py`/`memory.py` is built at import time), `OWNER_USER_ID`.
  - After saving one of these, a banner shows "Restart required" with a Restart button.
  - Restart calls `request_stop`, the same graceful shutdown as SIGTERM. pm2 (`autorestart: true`) starts the process again.
  - The page notes that without pm2 the bot stays stopped.

**Contacts `/contacts`, `/contacts/<chat_id>`**
- **List page:** display name, relationship, nickname, last message time, paused flag. A search box filters on name, nickname, @username and chat id. Filtering runs in Python over the new `db.list_chats()`, which covers every chat with inbound messages, tagged or not.
- **Detail page** edits:
  - relationship (dropdown of `commands.VALID_RELATIONSHIPS`), nickname, per-chat pause;
  - DB note (`persona_extra`) and the full text of `prompts/contacts/<chat_id>.txt`;
  - memory entries: list, add (`db.add_memory`), expire (`db.expire_memory`).
- The detail page also shows the style fingerprint read-only, the last 20 messages, and an "Extract now" button. The button calls `db.enqueue`, so the existing memory worker picks it up within about 15s and no LLM call blocks the request.
- Every save calls `prompts.clear_cache()`.
- `chat_id` must parse as an integer. The active connection comes from `commands._active_conn_id()`, which keeps the existing owner filter.

**Prompts `/prompts`**
- Editors for `prompts/about_me.txt` and `prompts/personas/<rel>.txt`, one per relationship in `VALID_RELATIONSHIPS`.
- When only `<rel>.example.txt` exists, it pre-fills the editor. Saving writes the real `<rel>.txt`, which is gitignored and never overwrites the example.
- File paths are built only from the relationship whitelist, and from integer chat ids on the contact page. User input never forms a path, so traversal is impossible.
- Every save calls `prompts.clear_cache()`.

**Drafts `/drafts`**
- Lists pending drafts: contact, their message, the draft, and the expiry time.
- Actions: **Send** and **Skip**. The draft sits in an editable textarea. Send uses status `approved` when the text is unchanged and `edited` when it differs.
- All three go through `commands._resolve_pending`. Its first parameter changes from `ctx` to `bot`, since `ctx.bot` is the only thing it used; the Telegram callers pass `ctx.bot`.
- The atomic `db.claim_pending` keeps double-submits from sending twice.

## 4. Files

New:
- `secretary/setup.py`: the wizard. The dashboard reuses its `mask`, `merge_env` and network checks.
- `secretary/dashboard/__init__.py` (empty).
- `secretary/dashboard/auth.py`: tokens, sessions, guards.
- `secretary/dashboard/web.py`: templates, render/redirect helpers.
- `secretary/dashboard/app.py`: factory, middleware, login/logout, server.
- `secretary/dashboard/pages.py`: Status, Settings, restart.
- `secretary/dashboard/contacts.py`: Contacts and Prompts.
- `secretary/dashboard/drafts.py`.
- `secretary/dashboard/templates/*.html`: base, login, status, settings, restarting, contacts, contact, prompts, drafts.
- `secretary/dashboard/static/style.css`.
- `scripts/smoke_setup.py` (wizard helpers) and `scripts/smoke_dashboard.py` (DB helpers and routes).

Changed:
- `secretary/config.py`, `.env.example`: dashboard settings.
- `secretary/__main__.py`: start and stop the dashboard task, pass `request_stop`.
- `secretary/db.py`: `pop_state`, `get_stats`.
- `secretary/commands.py`: `/dashboard`; `_resolve_pending(bot, …)`; `on_stats` uses `db.get_stats`.
- `scripts/setup-ubuntu.sh`: call the wizard.
- `README.md` (Setup section), `CLAUDE.md` (layout + notes).

## 5. Error handling

- **Dashboard.** Validation errors re-render the form with the message. An unexpected exception in a route returns a 500 page; uvicorn isolates it per request, so it never stops the bot loop.
- **`.env` writes.** Back up first. If the write fails, the in-memory `settings` stays unchanged.
- **Wizard.** Every network call has a timeout and the retry/skip choice. Nothing is written until the final confirmation.

## 6. Testing

The repo uses smoke scripts, not pytest. This design follows that convention.

**`scripts/smoke_dashboard.py`** drives the app through `httpx.ASGITransport`. That keeps it on the same event loop as the aiosqlite connection. It uses a temp DB and a temp `.env`, stubs the network checks, and asserts:
- GET `/` without a session redirects to `/login`;
- a wrong `Host` returns 400; a POST without a matching `Origin` returns 403;
- a login token works once, a second use fails, and an expired token fails;
- a Settings POST writes the expected `bot_state` keys;
- a persona name outside the whitelist (e.g. `../x`) is rejected;
- rendered HTML never contains the raw token or API key.

**Wizard helpers** get assert-based checks (in the same script or a `__main__` self-check) for:
- matching `/start <code>` in a `getUpdates` payload;
- masking;
- the `.env` merge that preserves unknown keys, with a backup.

**Existing checks:** `scripts/smoke_core.py` and the import check `uv run python -c "import secretary.__main__"` must still pass.

**Manual check:** run the wizard against a real bot, start the bot, open the link through the tunnel, and click through every page with Chrome DevTools.

## 7. Build order

Each phase ships on its own:

1. **P1:** setup wizard plus the `setup-ubuntu.sh` change.
2. **P2:** dashboard core: runtime, auth and guards, `/dashboard` command, Status, Settings.
3. **P3:** Contacts, Prompts, memory.
4. **P4:** Drafts.

## Out of scope

- Public bind, HTTPS or reverse-proxy support; multi-user or roles.
- Telegram Mini App dashboard; the Nous-style hosted QR / managed-bot setup.
- A CLI command to mint login links (`/dashboard` and re-running setup cover it).
- Dashboard i18n (UI in English; content fields accept any language).
- Moving the `webtest/` replay tester into the dashboard.
