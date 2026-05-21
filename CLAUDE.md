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
3. per-chat pause (`/pause_chat`)
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

- Tagging: `/who <chat_id> <relationship> [nickname]`, `/contacts`, `/find <query>`
- Per-chat persona: drop file at `prompts/contacts/<chat_id>.txt`, then `/reload_prompts`; or `/note <chat_id> <text>` for DB-backed addendum.
- Memory: `/memory <chat_id>`, `/remember`, `/forget`, `/extract <chat_id>`, `/style <chat_id>`
- HITL: `/approval on|off`, `/innercircle on|off`, `/pending`, `/approve_<id>`, `/edit_<id>`, `/skip_<id>`
- Live tuning (no restart): `/delay`, `/away_delay`, `/cooldown`, `/voice on|off`
- Maintenance: `/preview <text>`, `/say <chat_id> <text>`, `/backup`, `/forget_chat <chat_id>`

## Deploy

Ubuntu + pm2. See `scripts/setup-ubuntu.sh` for one-shot install, `ecosystem.config.cjs` for the pm2 process. Day-to-day: `pm2 start ecosystem.config.cjs` / `pm2 logs tg-secretary`.

## Secrets

`.env` is gitignored. `.env.example` is scrubbed (placeholders only). Whoever has `.env` has full impersonation power for the configured Telegram account — rotate via `@BotFather /revoke` + OpenRouter dashboard if leaked.
