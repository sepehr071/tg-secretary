# Hosted platform for tg-secretary — design

Date: 2026-10-06
Status: approved in chat, pending written-spec review

## Goal

Non-technical users get their own tg-secretary without touching a server. They log in to a Persian website with Telegram, give us a bot (one tap or a pasted token), fill in who they are and how they talk, and we run the bot for them. We pay OpenRouter and charge users manually.

Success for v1 (invite-only beta, under 50 users):
- A new user goes from landing page to "connected" in under 10 minutes with no terminal and no BotFather knowledge.
- Each user's bot runs in isolation; one crash or bad prompt affects only that user.
- LLM spend per user is capped by their paid credit; no user can spend past it.
- Telegram Bot Developer Terms §5.4 obligations (disclosure, consent for third-party API use, retention statement) are met before any message reaches OpenRouter.

## Decisions (from interview)

| Topic | Decision |
|---|---|
| LLM cost | We pay OpenRouter, users buy credit |
| Scale | Invite-only beta, < 50 users, one server |
| Website language | Persian only (RTL) |
| Login | Telegram Login (OIDC) |
| Payment | Manual for beta (card-to-card / crypto), admin records it |
| Bot onboarding | Managed Bots one-tap link, with paste-token fallback |

## Research findings this design relies on

- Telegram Premium is no longer required for business bots (Bot API 10.0, 2026-05-08). Users attach the bot under **Settings > Chat Automation**. The bot needs **Secretary Mode** enabled in BotFather.
- `BusinessBotRights.can_reply` only allows replies in chats with an incoming message in the last 24 h. Existing code already reads `rights.can_reply` (handlers.py).
- Managed Bots (Bot API 9.6): a manager bot sends `https://t.me/newbot/{manager}/{suggested_username}?name=...`; on confirmation it receives a `managed_bot` update and fetches the token with `getManagedBotToken`. `replaceManagedBotToken` rotates it.
- Telegram Login is OIDC (Authorization Code + PKCE, discovery at `https://oauth.telegram.org/.well-known/openid-configuration`). The verified `id_token` carries the Telegram user id. Client ID/secret and allowed URLs come from BotFather's Login Widget settings.
- OpenRouter management API: `POST /api/v1/keys` with `limit`, `PATCH /api/v1/keys/{hash}` for `limit` and `disabled`, `GET /api/v1/keys/{hash}` returns `usage` and `limit_remaining`. `limit_reset` can be null (no auto reset).
- Bot Developer Terms §5.4: must state truthfully what the bot does and what data is kept, must not send business message content to third parties (third-party APIs included) without the user's authorization, must not hide bot activity from the owner. §4.5: the token owner stays liable for actions taken with the token.
- Current code is single-tenant (global `settings`, one DB connection, import-time LLM clients) but runs per tenant today when given its own working directory with `.env`, `prompts/` and `secretary.db`.

Unconfirmed, to verify during planning:
- Whether the manager bot can enable Secretary Mode on a managed bot, or the user must toggle it in BotFather. The paste-token path covers either case.
- Exact fields of the `managed_bot` update (creator user id).

## Architecture

Approach: control plane plus one process per tenant. The bot code stays single-tenant. A multi-tenant rewrite (one process, webhooks) is out of scope until the user count outgrows one server.

```
                 Caddy (HTTPS, public domain)
                          |
                 platform (FastAPI, :8700)
          /          |             |              \
  public site   onboarding     admin page     /app/* reverse proxy
                          |                          |
                 platform bot (PTB, polling)     tenant dashboard (127.0.0.1:<port>)
                          |
          pm2:  tgs-<id>  tgs-<id>  ...   (python -m secretary, cwd = tenants/<id>/)
                          |
                 OpenRouter (one sub-key per tenant)
```

### Components

**`hosting/` package (new)** — not named `platform`, which would shadow the stdlib module.
- FastAPI app with Jinja templates, Persian RTL, served on 127.0.0.1 behind Caddy.
- `hosting.db` (SQLite, WAL). Tables:
  - `users(tg_id PK, first_name, username, created_at, is_admin)`
  - `tenants(id PK, owner_tg_id (one live tenant per user), bot_id UNIQUE, bot_username, managed, status, profile_done, dashboard_port UNIQUE, proxy_secret, or_key_hash, warned_at_limit, last_error, created_at)`. `status` is one of `draft`, `awaiting_credit`, `running`, `stopped`, `deleted`.
  - `payments(id PK, tenant_id, amount_usd, paid_amount_text, note, admin_tg_id, created_at)`
  - `consents(id PK, tg_id, version, accepted_at)`
- Sessions: hashed tokens in `platform.db`, same pattern as `secretary/dashboard/auth.py`.
- Config via pydantic-settings, a separate env file (`hosting.env`): platform bot token, OIDC client id/secret, OpenRouter management key, public URL, tenants root, admin tg id, trial credit amount (default 0).

**Platform bot (inside the platform process)**
- Owns the Telegram Login client and the Managed Bots manager role.
- Handles `managed_bot` updates, fetches the token, and attaches it to the waiting tenant.
- Sends low-credit warnings (20 % left) and "out of credit" notices to users who started it.

**Tenant runtime**
- Directory `tenants/<id>/` containing `.env` (chmod 600), `prompts/` (copied from the tracked example personas plus the user's `about_me.txt`), `secretary.db`, `logs/`.
- `.env` holds `TG_BOT_TOKEN`, `OPENROUTER_API_KEY` (the tenant's sub-key), `OWNER_USER_ID`, `DASHBOARD_PORT`, `DASHBOARD_PROXY_SECRET`, `HOSTED=1`.
- Process managed with the pm2 CLI (`pm2 start ... --name tgs-<id> --cwd tenants/<id>`, `pm2 stop`, `pm2 delete`, `pm2 save`), called from the platform with `subprocess`. A pm2 wrapper module is the only place that shells out.

**Changes to the existing bot (`secretary/`)**
- `HOSTED=1` setting:
  - Dashboard Settings page hides and refuses edits to the bot token, OpenRouter key and owner id. The restart button stays.
  - Dashboard accepts a request when the `X-Platform-Auth` header matches `DASHBOARD_PROXY_SECRET` (constant-time compare), in place of its own login.
- Dashboard root-path prefix (`DASHBOARD_ROOT_PATH`, default empty): templates, redirects and `/static` use the prefix so it works under `/app/`.
- Dashboard templates get Persian strings. Hosted users see Persian; the local single-user mode keeps working.
- `llm.py`: on OpenRouter 402 (credit exhausted), skip the reply and notify the owner once per hour instead of retrying.

### Data flows

**Onboarding**
1. User opens the site and logs in with Telegram (OIDC). The platform verifies the `id_token` server-side and creates a `users` row.
2. Consent page: what the bot does, that message text goes to OpenRouter-hosted models, what is stored and for how long, how to delete. Acceptance is stored in `consents` with a version. No tenant is created without the current consent version.
3. Bot step:
   - One tap: platform shows the Managed Bots link. On the `managed_bot` update it fetches the token.
   - Fallback: user pastes a BotFather token. The platform calls `getMe` and rejects tokens whose `bot_id` already belongs to another tenant.
   - Then the page tells the user to enable Secretary Mode if verification shows it is needed.
4. Profile step: short Persian form (name, how you write, things the bot must never say). The platform writes it to `prompts/about_me.txt`.
5. Credit: tenant moves to `awaiting_credit` and the page shows payment instructions. With a nonzero trial credit, it skips to step 6.
6. Activation (admin records a payment, or trial applies):
   - Create the OpenRouter sub-key with `limit` = credit and `limit_reset` null.
   - Write `.env`, allocate a dashboard port, and start the pm2 process.
   - Status becomes `running`.
7. Connect step: the page says "Settings > Chat Automation > pick @your_bot" and polls the tenant DB `connections` table (read-only) until a connection for the owner appears, then shows "connected".

**Day-to-day**
- `/app/*` on the platform checks the platform session, looks up the user's tenant, and proxies to `127.0.0.1:<dashboard_port>` with `X-Platform-Auth`. All existing dashboard features (contacts, memory, prompts, drafts, live settings) come through unchanged.
- Bot DM commands keep working because the owner talks to their own bot.

**Billing**
- Admin page: list tenants with usage and remaining credit from the OpenRouter key API, and a form to record a payment (USD credit plus free-text note of what was paid).
- Recording a payment `PATCH`es the sub-key `limit` to old limit + amount, and starts the tenant if it was `awaiting_credit`.
- A periodic job (every 30 min) reads key usage and sends the 20 % warning once per top-up.

**Deletion**
- User clicks delete and confirms. The platform stops and deletes the pm2 process, disables the sub-key, deletes `tenants/<id>/`, and marks the row `deleted`. For managed bots it calls `replaceManagedBotToken` so the old token dies.

### Error handling

- pm2 command fails: tenant stays in its previous status, error shown on the admin page, nothing half-written left in `.env`. Directory writes go to a temp path first, then rename.
- OpenRouter key creation fails: activation aborts before the process starts. No payment is lost; the payment row exists and activation can be retried from the admin page.
- Tenant process crash loops: pm2 restarts it. The admin page shows pm2 status and restart count.
- Token revoked by the user in BotFather: the tenant process fails `getMe`. The admin and user pages show "bot token invalid" with a paste-new-token action.
- Platform down: tenant bots keep running (separate pm2 processes). Only the website and proxy are unavailable.

### Security

- Secrets: bot tokens and sub-keys live only in tenant `.env` files (chmod 600, owned by the service user). `hosting.db` stores the per-tenant proxy secret (the proxy must send it, so it cannot be a hash; same disk and trust level as the tenant `.env`) and OpenRouter key hash ids, never the keys. The OpenRouter management key lives in `hosting.env` only.
- Tenant dashboards bind to 127.0.0.1 only. The proxy secret is per tenant, so one tenant's secret cannot open another's dashboard.
- POSTs through the proxy keep the existing Origin check, with the public URL as the allowed origin.
- Session cookie: `Secure`, `HttpOnly`, `SameSite=Lax` (needed for the OIDC redirect back).
- Admin routes require `is_admin`.
- The server runs outside Iran (Telegram is blocked there). Staging server first; turkey_vps production is not touched by this work.
- Logs: tenant pm2 logs may contain the bot token (httpx `getFile` URL, see the CLAUDE.md foot-gun). The bot already sets the httpx logger to WARNING; the hosting process does the same.

## Testing

- `scripts/smoke_platform.py`, offline, same style as `scripts/smoke_dashboard.py`:
  - OIDC callback with a stubbed token verifier creates a user.
  - Onboarding refuses to create a tenant without consent.
  - Pasted token already used by another tenant is rejected.
  - Payment recording calls the OpenRouter PATCH with old limit + amount (`httpx.MockTransport`).
  - Activation writes `.env` with mode 600 and calls a fake pm2 runner with the right args.
  - Proxy refuses requests without a platform session and forwards the correct `X-Platform-Auth`.
- `scripts/smoke_dashboard.py` gains cases for `HOSTED=1` (secret fields hidden and refused) and `DASHBOARD_ROOT_PATH`.
- `scripts/smoke_core.py` gains the 402 handling case.
- Manual end-to-end on staging with a real test Telegram account: login, managed bot, connect, reply, top-up, delete.

## Out of scope (v1)

- Automatic payment gateway (Zarinpal, crypto, Stars)
- Webhooks and the single-process multi-tenant refactor
- English UI
- Per-user model choice (all tenants use the configured defaults)
- Usage analytics beyond OpenRouter key usage

## Build split (for the plan)

Contract first (DB schema, tenant `.env` keys, proxy header, pm2 wrapper interface), then three parallel workers with disjoint paths:
1. Platform backend: `hosting/` app, DB, OIDC, Managed Bots, OpenRouter client, pm2 wrapper, admin, proxy.
2. Platform frontend: Persian RTL templates and static files for site, onboarding, admin.
3. Bot changes: `secretary/` `HOSTED` mode, root path, Persian dashboard strings, 402 handling.

Then Caddy config, staging deploy, and the manual end-to-end check.
