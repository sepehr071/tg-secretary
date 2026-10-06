# tg-secretary

Personal Telegram Business autoresponder. Single-user per process; `hosting/` runs one process per user behind a Persian website with one shared bot. OpenRouter-backed LLM, OGG voice transcription via `openai/whisper-large-v3`, per-contact memory, file-backed persona overrides, HITL approval mode, inner-circle safety gate.

**Source:** https://github.com/sepehr071/tg-secretary

## Architecture

- Python 3.13, `python-telegram-bot[ext] >= 22.5`, `openai` SDK pointed at OpenRouter, `aiosqlite`, `pydantic-settings`, `httpx`.
- One shared SQLite connection (WAL mode). Tables: `connections`, `messages`, `chat_summaries`, `contact_overrides`, `contact_memory`, `extraction_queue`, `pending_replies`, `bot_state`.
- Reply model: `OPENROUTER_MODEL` (default `openai/gpt-5.5`). Extractor/summarizer: `EXTRACTOR_MODEL` (default `google/gemini-3.1-flash-lite`). Voice: `WHISPER_MODEL` (default `openai/whisper-large-v3`).
- Async background worker drains `extraction_queue` to build durable per-contact memory.

## Layout

```
secretary/
├── __main__.py       entrypoint, registers handlers, starts memory worker
├── config.py         pydantic-settings env loader
├── db.py             shared aiosqlite conn, WAL, all CRUD
├── handlers.py       business_message routing, race guards, away mode, HITL fork
├── commands.py       owner-only / commands (DM the bot)
├── innercircle.py    gf/family/bff/close_friend emotional-keyword + length + silence gate
├── voice.py          OpenRouter Whisper transcription (httpx)
├── memory.py         extraction worker + style fingerprint + summarization
├── llm.py            reply generation (delimiter-wrapped, retry-on-empty)
├── prompts.py        PERSONA registry + file-backed per-contact prompts
├── setup.py          first-run wizard (validates token/key, owner /start code, writes .env)
└── dashboard/        owner web UI served in-process on 127.0.0.1
    ├── app.py        create_app, guard middleware, login/logout, uvicorn server task
    ├── auth.py       one-time login tokens + hashed sessions in bot_state, Host/Origin guards
    ├── pages.py      Status, Settings (simple view + "Advanced" switch; live bot_state + .env config), restart
    ├── contacts.py   contacts, memory, per-contact prompt files, persona/about_me editor
    ├── drafts.py     HITL queue (Send / Skip)
    ├── fa.py         Persian strings (DASHBOARD_LANG=fa), used via the `t` filter / web.t()
    └── web.py, templates/, static/ (style.css + Vazirmatn fonts, same palette as hosting/)

hosting/              hosted multi-user platform (see hosting/README.md)
├── app.py            FastAPI site: Telegram login, consent, profile, account, /admin payments
├── bot.py            the ONLY poller of the shared bot: routes updates, /start for strangers, credit alerts
├── router.py         update → owning tenant (by user / business_connection_id) → POST 127.0.0.1:<port>/_tg/update
├── proxy.py          /app/* → tenant dashboard with X-Platform-Auth
├── tenants.py        tenant dir/.env, OpenRouter key (absolute limit = sum of payments), pm2, delete
├── oidc.py           Telegram OIDC login + classic Login Widget fallback
├── pm2.py, openrouter.py, tg.py, db.py, config.py
├── templates/, static/  Persian RTL site
└── ecosystem.config.cjs, Caddyfile.example, README.md

prompts/
├── personas/         <relationship>.txt (gitignored, real) → falls back to tracked <relationship>.example.txt
├── contacts/         <chat_id>.txt — hand-tuned per-friend prompts
└── about_me.txt      owner self-facts, injected as ## About me into every prompt

webtest/              local FastAPI replay tester (uploads/<uuid>.db, model A/B/C compare + reasoning dropdown)
├── app.py            FastAPI endpoints + SSE replay stream
├── replay.py         per-turn fan-out over N (model, reasoning_effort) columns
└── static/, templates/

tests/
├── fixtures/
│   ├── scenarios/    *.json — synthetic + dumped scenarios; raw_*.json gitignored
│   └── rubrics/      <rel>.json — judge rubric per relationship
├── snapshots/        gitignored — assembled prompt + reply + judge verdict per run
└── judge_prompt.md   judge system prompt + strict JSON schema

scripts/
├── setup-ubuntu.sh
├── dump_fixtures.py  secretary.db → tests/fixtures/scenarios/*.json (anonymized or raw)
└── test_prompts.py   per-scenario runner + LLM judge
```

## Reply pipeline (text)

1. owner-skip guard (owner-typed messages stored, not replied to)
2. global pause (`/pause`)
3. per-chat pause (`/pause_chat`)
4. quiet-hours window (`/quiet`)
5. owner-active cooldown (`OWNER_ACTIVE_COOLDOWN_SECONDS`, live-tunable via `/cooldown`)
6. delay: 30s default, 5s in away mode (bot replied last without owner preemption)
7. race re-check (owner typed during sleep → abort)
8. inner-circle gate (on by default, `/innercircle off` to disable) → if `relationship in {gf,bff,family,close_friend}` AND (emotional keyword OR >200 chars OR >24h silence) → draft to HITL queue, no auto-send
9. approval mode (`/approval on`) → same as #8
10. LLM call (system prompt assembled from persona registry + per-contact .txt + DB persona_extra + memory block + style fingerprint, user msg wrapped in `<<<contact_message>>>` delimiters)
11. final race re-check before send
12. send via Business connection, store as `via_bot=1`, enqueue memory extraction

## Voice path

`get_file` → bytes → POST `https://openrouter.ai/api/v1/audio/transcriptions` (model `openai/whisper-large-v3`, base64 OGG payload) → transcript → standard text pipeline. Voice >120s or transcribe-off → owner-notify, no auto-reply.

## Owner control surface

All commands are sent to the bot's own DM (not via Business connection). Owner-only — guarded by `update.effective_user.id == settings.owner_user_id`. See `/help` in-bot for the full list. Highlights:

- Tagging: `/who <chat_id> <relationship> [nickname]`, `/contacts`, `/find <query>`
- Per-chat persona: drop file at `prompts/contacts/<chat_id>.txt`, then `/reload_prompts`; or `/note <chat_id> <text>` for DB-backed addendum.
- One-line facts: `/prompt <chat_id> <fact>` appends `- <fact>` to `prompts/contacts/<chat_id>.txt` and clears the prompt cache.
- Owner self-facts: edit `prompts/about_me.txt`; injected as `## About me` section into every assembled prompt.
- Memory: `/memory <chat_id>`, `/remember`, `/forget`, `/extract <chat_id>`, `/style <chat_id>`
- HITL: `/approval on|off`, `/innercircle on|off`, `/pending`, `/approve_<id>`, `/edit_<id>`, `/skip_<id>`
- Live tuning (no restart): `/delay`, `/away_delay`, `/cooldown`, `/voice on|off`
- Maintenance: `/preview <text>`, `/say <chat_id> <text>`, `/backup`, `/forget_chat <chat_id>`
- Web dashboard: `/dashboard` replies with a one-time login link (1 h). Over SSH: `ssh -L 8780:127.0.0.1:8780 user@server`. It writes the same `bot_state` keys as the commands, so both surfaces always agree.

## Deploy

Ubuntu + pm2. See `scripts/setup-ubuntu.sh` for one-shot install, `ecosystem.config.cjs` for the pm2 process. Day-to-day: `pm2 start ecosystem.config.cjs` / `pm2 logs tg-secretary`.

## Secrets

`.env` is gitignored. `.env.example` is scrubbed (placeholders only). Whoever has `.env` has full impersonation power for the configured Telegram account — rotate via `@BotFather /revoke` + OpenRouter dashboard if leaked.

## Testing

LLM-judged prompt-management suite. Real OpenRouter calls, no pytest, no mocks. Catches voice drift before it ships.

- `scripts/dump_fixtures.py` — `secretary.db` → `tests/fixtures/scenarios/*.json`. Anonymized by default (sha256-keyed alias swap + phone/email/URL redaction). `--no-anonymize` writes real content into `raw_*.json` (gitignored by prefix). Anonymization preserves emojis/register/code-switching/swears — only PII gets swapped.
- `scripts/test_prompts.py` — for each scenario: temp DB seed → `prompts.load_system_prompt` assembly → real reply call → structural checks (no leaked `reply:`, no `(translation)` tail, no markdown, no wrap quotes) → judge call (default `anthropic/claude-sonnet-4-6`) with per-relationship rubric → snapshot. Exit 0 only if every scenario passes.
- Verdict: `weighted_avg ≥ pass_threshold` AND every dimension `≥` its `min` AND `red_flags_hit == []`. Red flags hard-gate (e.g. one `**bold**` token = fail).
- Style-fingerprint guard: catches `recurring_phrases` key before any LLM call (the feedback-loop foot-gun).
- Cost envelope: ~$0.035/scenario for full grade; ~$0.015 with `--no-judge`. Full 7-scenario starter run ~$0.25.

Workflow:
```bash
uv run python scripts/dump_fixtures.py --no-anonymize --limit-per-relationship 5
uv run python scripts/test_prompts.py --no-judge                  # fast iteration
uv run python scripts/test_prompts.py                             # graded run
uv run python scripts/test_prompts.py --scenario raw_gf_a1b2c3d4 --verbose
```

Snapshots at `tests/snapshots/<name>.snapshot.txt` show the full assembled persona stack + reply + judge JSON — diff them after a persona-file edit to see what changed.

## Working notes for future Claude sessions

### Foot-guns
- OpenRouter `reasoning.enabled=false` is rejected by Gemini 3.5+ ("Reasoning is mandatory"). Use `extra_body={"reasoning": {"effort": "minimal", "exclude": True}}` for cross-model compat.
- OpenRouter `reasoning` param: `effort ∈ {xhigh, high, medium, low, minimal, none}` (OpenAI-style) OR `max_tokens: <int>` (Anthropic-style) — never both. Always include `exclude: true` so reasoning tokens don't leak into the reply content. `secretary/llm.py:generate_reply` accepts a `reasoning_effort` kwarg; default is `minimal+exclude` (the floor Gemini 3.5+ won't reject).
- The hosting package is `hosting`, never `platform` — `platform` shadows the stdlib.
- Starlette `Jinja2Templates.TemplateResponse` signature: `templates.TemplateResponse(request, "name.html", {...})` — `request` is now positional. Old `TemplateResponse("name.html", {"request": request, ...})` form raises `TypeError: unhashable type: 'dict'` on Starlette ≥ 0.29.
- Telegram `getFile` URL embeds the bot token; httpx logs it at INFO. Treat `pm2 logs` as a token-leak surface — `@BotFather /revoke` if a paste escapes.
- `cur.lastrowid` is `int | None`. Guard with `if lastrowid is None: raise RuntimeError(...)` before returning from INSERT helpers.
- OpenAI SDK `messages=` trips Pyright on `list[dict[str, Any]]`. Suppress with `# type: ignore[arg-type]` on the arg line itself.
- `.gitignore` must include `*.db-wal` + `*.db-shm` (WAL is enabled).
- `uv.lock` is committed (NOT gitignored) so `uv sync --frozen` works in `run.sh`.
- Persian/Arabic in Windows stdout crashes on cp1252; prefix one-off CLI runs with `PYTHONIOENCODING=utf-8`.
- Closed vocab lists and example-response tables in `prompts/personas/<rel>.txt` ("Pet names: X, Y, Z. Never invent new ones." / "She: X → you: Y") make the reply model treat them as a lookup table → repetitive, robotic output. Reframe as illustrative range ("examples of the register, vary your own phrasing"), never as a fixed inventory or stimulus-response pair.
- Never add fields to `memory.py:STYLE_SYSTEM` that capture phrasings the BOT outputs (e.g. `recurring_phrases`). Creates a feedback loop: fingerprint records bot drift → injected back as `## Style` system prompt → bot uses it more → 30-day lock-in until next refresh. Only capture durable owner-voice signals (length, formality, pet names, signature open/close).
- `secretary/llm.py` defaults — `temperature=0.9` (retry 0.7) + `frequency_penalty=0.4` + `presence_penalty=0.2` — are tuned for vocabulary variety. The earlier 0.65/0.4 with no penalties is what produced the "robotic + repetitive" feel; don't quietly lower without a deliberate reason.
- `dump_fixtures.py --no-anonymize` outputs `tests/fixtures/scenarios/raw_*.json` — gitignored by prefix, so they don't leak via git. BUT `test_prompts.py` still ships those fixtures to OpenRouter (reply model + judge model) on every run. .gitignore is not a network filter; if real-data fidelity matters less than that round-trip, stay on anonymized fixtures.
- `prompts.load_system_prompt` uses `str.replace`, NOT `str.format`, to substitute `{owner_first_name}` — the assembled prompt contains literal `{`/`}` characters (JSON-style style_fingerprint, memory entries that quote user text). Switching to `.format()` will KeyError on any scenario with memory containing braces; the test suite exercises this implicitly.

### Internal patterns
- **Live env override**: read `db.get_state(key)` first, fall back to `settings.X`. See `handlers._live_int(...)` and `_voice_enabled()`. New tunables: add the live-read helper + a `/cmd` in `commands.py`.
- **Persona stack** (assembled in `prompts.load_system_prompt`): in-code DEFAULT → `prompts/about_me.txt` (## About me) → `prompts/personas/<rel>.txt` (else `<rel>.example.txt`) → `prompts/contacts/<chat_id>.txt` → DB `persona_extra` → memory block → style fingerprint. `prompts.clear_cache()` flushes the mtime cache after edits.
- **Profile capture**: call `_capture_profile(conn_id, chat_id, msg)` in every inbound entry point (text / voice / non-text) after the owner-skip guard, before persisting the row.
- **Owner-only command guard**: every `on_<cmd>` starts with `if not _is_owner(update): return`.
- **Human-texting cleanup**: `llm._clean_output` runs `_strip_terminal_period` last — it deletes a lone trailing `.` (the #1 machine-written tell, doubly so in Persian) while preserving `...`/`..`/`…` and `؟`/`?`/`!`. The prompt forbids trailing periods too (models leak them anyway, hence the deterministic backstop). The DEFAULT prompt also mandates colloquial Persian spelling (میدونم not می‌دانم, no tidy ZWNJ everywhere) — formal book-Persian reads robotic.

- **Concurrency**: `Application.builder().concurrent_updates(True)` is load-bearing — with serial updates the race-guard `asyncio.sleep` hides the owner's own messages until the delay ends (double replies). Bursts in one chat collapse via `handlers._chat_gen`: each inbound bumps it, a pipeline that sees a newer value after its delay / LLM call drops out. Contact deletes bump it too (cancels the in-flight reply).
- **Owner-only connections**: `db.get_connection` and `commands._active_conn_id` filter on `owner_user_id = settings.owner_user_id`; `on_business_connection` ignores non-owners. Anyone can attach a business bot — never drop that filter.
- **HITL drafts**: resolve through `commands._resolve_pending` → `db.claim_pending` (atomic `UPDATE ... WHERE status='pending' AND expires_at > now`) so a double-tapped Send can't send twice.
- **Proxy env for local runs**: Python ignores the OS proxy setting. On a host where direct egress is blocked, export `HTTPS_PROXY`/`HTTP_PROXY` first — otherwise OpenRouter calls fail with `403 Access denied by security policy`, which looks like an auth error but isn't.
- `allowed_updates` in `__main__.py` must list `callback_query`, or every inline button silently does nothing.
- **Dashboard runtime**: runs as a `uvicorn.Server` task in the bot's asyncio loop. `_Server.capture_signals` is a no-op so uvicorn doesn't steal SIGINT/SIGTERM from the bot. A busy port logs an error and the bot keeps running (uvicorn's `sys.exit(1)` is caught in `run_server`). Restart button = graceful stop; pm2 brings it back.
- **Dashboard auth**: login only via one-time links (`/dashboard`, or the end of `secretary.setup`); the token sits in the URL fragment so it never hits a log. Tokens and sessions are sha256-hashed in `bot_state` under `dash_login:` / `dash_session:`. Guards: loopback `Host` (any port, so `ssh -L 9000:...` works) or the hostname of `DASHBOARD_PUBLIC_URL`, plus `Origin` (http or https) on every POST. Default bind is 127.0.0.1 via `ssh -L`; public bind is opt-in (`DASHBOARD_HOST=0.0.0.0` + `DASHBOARD_PUBLIC_URL`). Over plain http the session is sniffable; keep the default loopback, and prefer an https proxy (the cookie turns `Secure` for https URLs).
- `secretary/setup.py` must not import `secretary.config` at module level (Settings() fails without `.env`). The dashboard reuses its `mask` / `merge_env` / network checks; tests stub them via the `setup` module attribute.
- `smoke_*.py` scripts must close the DB in `finally`: an open aiosqlite thread hangs the process when an assert fails.

### Hosted platform (hosting/)
- **Model**: one control plane (`python -m hosting`, pm2 `tg-hosting`, 127.0.0.1:8700 behind nginx/Caddy) + one `python -m secretary` per user (pm2 `tgs-<id>`, cwd `tenants/<id>/`, own `.env`, `secretary.db`, `prompts/`). Bot code stays single-tenant; `HOSTED=1` switches its behaviour.
- **One shared bot**: every user connects the platform bot in Telegram Settings > Chat Automation. Secretary Mode is toggled ONCE by the operator in BotFather (no API can toggle it for another bot; that's why per-user managed bots were dropped). The platform bot must be a dedicated bot — never a token another process already polls (two pollers split updates; prod lost messages once).
- **Update routing**: `hosting/bot.py` is the only `getUpdates` caller. `router.dispatch` forwards to tenants with status `running` only; strangers are dropped (one "register on the site" DM per account with no tenant). DMs/callbacks route by `from.id` in private chats only. Each update logs `update <id> <kind> -> forwarded|dropped|platform` (never contents).
- **Tenant intake (HOSTED=1)**: no polling (`Application.builder().updater(None)`); `POST /_tg/update` on the tenant dashboard puts the update on PTB's `update_queue`. It requires `X-Platform-Auth` AND `X-Platform-Route: update`; the `/app/` proxy forwards only `content-type`/`accept` and 404s any normalized `/_tg*` path, so users can't inject updates. Never relax either check — with a shared bot a forged connection could reach another user's chats.
- **Connections the tenant never saw** (user connected before paying / while stopped): `handlers._connection` fetches `get_business_connection` in hosted mode and stores it only if `user.id == OWNER_USER_ID`.
- **Rights**: a connection without `can_reply` makes the secretary skip every message (`not allowed to reply`). Account page (`tenants.connection_state`: none / no_reply / ok) and the owner DM (Persian when `DASHBOARD_LANG=fa`) tell the user to enable reply permission under Chat Automation.
- **Owner-active cooldown** (default 600 s) skips a chat the owner typed in recently — looks like "nothing happened" during testing; `/cooldown 0` to test, `/cooldown off` after.
- **Money**: `tenants.sync_limit` sets the OpenRouter key limit to the ABSOLUTE sum of the tenant's payments (idempotent under retries/timeouts). Trial = payment row `trial-user-<tg_id>` (once per person). `top_up` needs `profile_done`; admin records payments with a per-row `client_ref`.
- **Consent** (Telegram Bot Developer Terms §5.4): no tenant/profile/activation before the current `CONSENT_VERSION` is accepted.
- **Secrets/logs**: tenant `.env` mode 600; `hosting.db` holds proxy secrets + key hashes, never keys. Don't log update/message contents (`_on_error` logs id + kind only) or Bot API URLs (token inside); log `type(e).__name__` for httpx errors. pm2 subprocesses get an env scrubbed of hosting settings.
- **Login**: OIDC when `OIDC_CLIENT_ID` is set (BotFather "switch to OpenID login", redirect `https://<domain>/auth/callback`); else classic Login Widget (`/setdomain`), verified by HMAC with sha256(bot token).
- **Dashboard in hosted mode**: served under `/app` (`DASHBOARD_ROOT_PATH`), Persian (`DASHBOARD_LANG=fa`), header auth instead of login links, token/key/owner fields hidden. All URLs/redirects use `{{ root }}` / `web.back()`.
- **Staging**: personal-france (`54.38.3.7`), `/opt/tg-hosting`, user `tgsec`, nginx site `monshi` → `https://monshi.sepehrradmard.ir`, platform bot `@monshi_platform_bot`. Deploy = `git archive HEAD` → upload → extract over `/opt/tg-hosting` → `pm2 restart tg-hosting tgs-<id>` as `tgsec`. `hosting.env` is server-only. That box also serves the live mcpfarsi site — only touch the `monshi` nginx site.

### Smoke tests
- Offline core logic (no .env, no network): `uv run python scripts/smoke_core.py`.
- Setup wizard helpers: `uv run python scripts/smoke_setup.py`.
- Dashboard routes + DB helpers (no network): `PYTHONIOENCODING=utf-8 uv run python scripts/smoke_dashboard.py`.
- Hosting platform (no network): `PYTHONIOENCODING=utf-8 uv run python scripts/smoke_hosting.py` — routing, onboarding, payments, proxy, login. Expected tracebacks in its output come from deliberate failure cases.
- Staging logs: `pm2 logs tg-hosting` (routing lines `hosting.bot: update …`) and `tenants/<id>/logs/err.log` (per-user secretary).
- Import-time crash: `uv run python -c "import secretary.__main__; print('OK')"`.
- Migration sanity: `init_db`, then `PRAGMA table_info(<table>)` for any migrated table.
- Log filter on Ubuntu: `pm2 logs tg-secretary --lines 200 | grep -iE "voice|whisper|httpx|llm"`.
- Prompt suite, no LLM cost: `uv run python scripts/test_prompts.py --no-judge` — exercises scenario load + DB seed + persona stack assembly + reply call + cleanup-pipeline structural checks.
- Prompt suite, full: `uv run python scripts/test_prompts.py` — adds judge grading + rubric verdict.
- Web tester: `PYTHONIOENCODING=utf-8 uv run python -m webtest` → http://127.0.0.1:8765/. Upload `secretary.db`, pick chat + slice, compare up to 3 models side-by-side with per-column reasoning effort.

### Deploy
- `pm2 start ecosystem.config.cjs` is the only command users need; `scripts/setup-ubuntu.sh` is interactive + re-runnable (asks to regenerate or keep existing `.env`).
- pm2 autostart: `pm2 startup systemd -u $USER --hp $HOME`, run the printed sudo line, then `pm2 save`.
