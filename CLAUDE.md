# tg-secretary

Personal Telegram Business autoresponder. Single-user. OpenRouter-backed LLM, OGG voice transcription via `openai/whisper-large-v3`, per-contact memory, file-backed persona overrides, HITL approval mode, inner-circle safety gate.

**Source:** https://github.com/sepehr071/tg-secretary

## Architecture

- Python 3.13, `python-telegram-bot[ext] >= 22.5`, `openai` SDK pointed at OpenRouter, `aiosqlite`, `pydantic-settings`, `httpx`.
- One shared SQLite connection (WAL mode). Tables: `connections`, `messages`, `chat_summaries`, `contact_overrides`, `contact_memory`, `extraction_queue`, `pending_replies`, `bot_state`.
- Reply model: `OPENROUTER_MODEL` (default `anthropic/claude-sonnet-4-6`). Extractor/summarizer: `EXTRACTOR_MODEL` (default `google/gemini-3.1-flash-lite`). Voice: `WHISPER_MODEL` (default `openai/whisper-large-v3`).
- Async background worker drains `extraction_queue` to build durable per-contact memory.

## Layout

```
secretary/
├── __main__.py       entrypoint, registers handlers, starts memory worker
├── config.py         pydantic-settings env loader
├── db.py             shared aiosqlite conn, WAL, all CRUD
├── handlers.py       business_message routing, race guards, away mode, HITL fork
├── commands.py       owner-only / commands (DM the bot)
├── innercircle.py    gf/family/bff emotional-keyword + length + silence gate
├── voice.py          OpenRouter Whisper transcription (httpx)
├── memory.py         extraction worker + style fingerprint + summarization
├── llm.py            reply generation (delimiter-wrapped, retry-on-empty)
└── prompts.py        PERSONA registry + file-backed per-contact prompts

prompts/
├── personas/         <relationship>.txt — gf, bff, friend, family, work, ...
└── contacts/         <chat_id>.txt — hand-tuned per-friend prompts
```

## Reply pipeline (text)

1. owner-skip guard (owner-typed messages stored, not replied to)
2. global pause (`/pause`)
3. per-chat pause (`/pause <chat_id>`)
4. quiet-hours window (`/quiet`)
5. owner-active cooldown (`OWNER_ACTIVE_COOLDOWN_SECONDS`, live-tunable via `/cooldown`)
6. delay: 30s default, 5s in away mode (bot replied last without owner preemption)
7. race re-check (owner typed during sleep → abort)
8. inner-circle gate (`/innercircle on`) → if `relationship in {gf,bff,family}` AND (emotional keyword OR >200 chars OR >24h silence) → draft to HITL queue, no auto-send
9. approval mode (`/approval on`) → same as #8
10. LLM call (system prompt assembled from persona registry + per-contact .txt + DB persona_extra + memory block + style fingerprint, user msg wrapped in `<<<contact_message>>>` delimiters)
11. final race re-check before send
12. send via Business connection, store as `via_bot=1`, enqueue memory extraction

## Voice path

`get_file` → bytes → POST `https://openrouter.ai/api/v1/audio/transcriptions` (model `openai/whisper-large-v3`, base64 OGG payload) → transcript → standard text pipeline. Voice >120s or transcribe-off → owner-notify, no auto-reply.

## Owner control surface

All commands are sent to the bot's own DM (not via Business connection). Owner-only — guarded by `update.effective_user.id == settings.owner_user_id`. See `/help` in-bot for the full list. Highlights:

- Tagging: `/who <chat_id> <relationship> [nickname]`, `/contacts`, `/find <query>`, `/senders [N]`
- Per-chat persona: drop file at `prompts/contacts/<chat_id>.txt`, then `/reload`; or `/note <chat_id> <text>` for a DB-backed addendum (overwrites).
- Memory: `/profile <chat_id> [refresh]` (narrative briefing — primary), `/prompt <chat_id> <fact>` (add a stacking fact), `/memory <chat_id>`, `/forget <memory_id>`, `/extract <chat_id>`, `/style <chat_id>`, `/purge <chat_id|all>`
- HITL: `/approval on|off`, `/innercircle on|off`, `/pending`, `/approve_<id>`, `/edit_<id>`, `/skip_<id>`
- Live tuning (no restart): `/delay`, `/away_delay`, `/cooldown`, `/voice on|off`
- Maintenance: `/preview <text>`, `/say <chat_id> <text>`, `/backup`, `/wipe <chat_id>` (purge messages+memory+summary)

## Deploy

Ubuntu + pm2. See `scripts/setup-ubuntu.sh` for one-shot install, `ecosystem.config.cjs` for the pm2 process. Day-to-day: `pm2 start ecosystem.config.cjs` / `pm2 logs tg-secretary`.

## Secrets

`.env` is gitignored. `.env.example` is scrubbed (placeholders only). Whoever has `.env` has full impersonation power for the configured Telegram account — rotate via `@BotFather /revoke` + OpenRouter dashboard if leaked.

## Working notes for future Claude sessions

### Foot-guns
- OpenRouter `reasoning.enabled=false` is rejected by Gemini 3.5+ ("Reasoning is mandatory"). Use `extra_body={"reasoning": {"effort": "minimal", "exclude": True}}` for cross-model compat.
- Telegram `getFile` URL embeds the bot token; httpx logs it at INFO. Treat `pm2 logs` as a token-leak surface — `@BotFather /revoke` if a paste escapes.
- `cur.lastrowid` is `int | None`. Guard with `if lastrowid is None: raise RuntimeError(...)` before returning from INSERT helpers.
- OpenAI SDK `messages=` trips Pyright on `list[dict[str, Any]]`. Suppress with `# type: ignore[arg-type]` on the arg line itself.
- `.gitignore` must include `*.db-wal` + `*.db-shm` (WAL is enabled).
- `uv.lock` is committed (NOT gitignored) so `uv sync --frozen` works in `run.sh`.
- Persian/Arabic in Windows stdout crashes on cp1252; prefix one-off CLI runs with `PYTHONIOENCODING=utf-8`.
- Closed vocab lists and example-response tables in `prompts/personas/<rel>.txt` ("Pet names: X, Y, Z. Never invent new ones." / "She: X → you: Y") make the reply model treat them as a lookup table → repetitive, robotic output. Reframe as illustrative range ("examples of the register, vary your own phrasing"), never as a fixed inventory or stimulus-response pair.
- Never add fields to `memory.py:STYLE_SYSTEM` that capture phrasings the BOT outputs (e.g. `recurring_phrases`). Creates a feedback loop: fingerprint records bot drift → injected back as `## Style` system prompt → bot uses it more → 30-day lock-in until next refresh. Only capture durable owner-voice signals (length, formality, pet names, signature open/close).
- `secretary/llm.py` defaults — `temperature=0.9` (retry 0.7) + `frequency_penalty=0.4` + `presence_penalty=0.2` — are tuned for vocabulary variety. The earlier 0.65/0.4 with no penalties is what produced the "robotic + repetitive" feel; don't quietly lower without a deliberate reason.

### Internal patterns
- **Live env override**: read `db.get_state(key)` first, fall back to `settings.X`. See `handlers._live_int(...)` and `_voice_enabled()`. New tunables: add the live-read helper + a `/cmd` in `commands.py`.
- **Persona stack** (assembled in `prompts.load_system_prompt`): in-code DEFAULT → `prompts/personas/<rel>.txt` → **narrative `profile`** (if synthesized) OR memory bullets (fallback for fresh contacts) → `prompts/contacts/<chat_id>.txt` → DB `persona_extra` → style fingerprint. The `profile` is a 200-400 word paragraph the extractor writes from `contact_memory` rows; once it exists it replaces the bullet list in-prompt. Built lazily by `memory.maybe_refresh_profile()`; gates on >=3 memories, 7d staleness, or 10 new memory rows. `prompts.clear_cache()` flushes the mtime cache after edits.
- **Profile capture**: call `_capture_profile(conn_id, chat_id, msg)` in every inbound entry point (text / voice / non-text) after the owner-skip guard, before persisting the row.
- **Owner-only command guard**: every `on_<cmd>` starts with `if not _is_owner(update): return`.

### Smoke tests
- Import-time crash: `uv run python -c "import secretary.__main__; print('OK')"`.
- Migration sanity: `init_db`, then `PRAGMA table_info(<table>)` for any migrated table.
- Log filter on Ubuntu: `pm2 logs tg-secretary --lines 200 | grep -iE "voice|whisper|httpx|llm"`.

### Deploy
- `pm2 start ecosystem.config.cjs` is the only command users need; `scripts/setup-ubuntu.sh` is interactive + re-runnable (asks to regenerate or keep existing `.env`).
- pm2 autostart: `pm2 startup systemd -u $USER --hp $HOME`, run the printed sudo line, then `pm2 save`.
