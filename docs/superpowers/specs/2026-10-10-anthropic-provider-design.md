# Anthropic provider (Claude Haiku 5.5) for the hosted platform

Date: 2026-10-10. Status: approved in chat, implemented in the same session.

## Goal

Hosted users run on the Anthropic API with `claude-haiku-5-5` only. OpenRouter is never
shown to users. Voice notes are off for now and the UI says "coming soon". The
OpenRouter code path stays for self-hosters and as a fallback.

## Decisions

- **Provider switch**: `ANTHROPIC_API_KEY` set → provider `anthropic`, else `openrouter`.
  `settings.provider` is the single source of truth; `OPENROUTER_API_KEY` becomes optional.
- **Model**: `ANTHROPIC_MODEL` (default `claude-haiku-5-5`) for replies, memory extraction,
  style fingerprint and summary rollups. No sampling params (Haiku 5.5 rejects them);
  adaptive thinking at `effort: low`, `max_tokens` leaves room for thinking.
- **Credit (hosted)**: one platform key. Every Claude call records tokens and USD into the
  tenant DB (`llm_usage`). The platform writes `CREDIT_LIMIT_USD` = sum of payments into the
  tenant `.env` and restarts the tenant on top-up. The tenant refuses to reply once
  `usage_total >= limit` (same owner notice as the old 402). The platform reads spend from
  the tenant DB read-only, so account, admin and the credit-alert loop keep their shape.
- **Voice**: with provider `anthropic` transcription is unavailable; `_voice_enabled()` is
  False, the dashboard switch is replaced by a "coming soon" note, and the privacy page says
  voice notes are not handled yet.
- **Wording**: user-facing text names no provider: "سرویس هوش مصنوعی".
- **Out of scope**: `webtest/`, `scripts/test_prompts.py`, `dump_fixtures.py` stay on
  OpenRouter (developer tools). Setup wizard unchanged (self-host path).

## Pricing table (Haiku 5.5, prompts ≤ 100K tokens)

input $0.10/MTok, output $0.50/MTok, cache read $0.01/MTok, cache write $0.125/MTok.

## Files

secretary: `config.py`, `db.py` (llm_usage), `claude.py` (new), `llm.py`, `memory.py`,
`handlers.py` (credit guard, voice gate), `dashboard/pages.py` + `settings.html` +
`status.html` + `fa.py`.
hosting: `config.py`, `tenants.py` (key/limit/credit), `app.py`, `bot.py`, templates
(`consent.html`, `privacy.html`), `hosting.env.example`, README.
tests: `smoke_core.py`, `smoke_dashboard.py`, `smoke_hosting.py`.
