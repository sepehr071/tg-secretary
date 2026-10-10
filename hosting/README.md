# Hosting platform

Control plane that lets users sign in with Telegram, connect one shared secretary bot under Telegram Settings > Chat Automation, and runs each user's secretary as a per-tenant pm2 process (`tgs-<id>`). The control plane is the only process polling the shared bot; it forwards each update to the owning tenant over loopback. Package name is `hosting`, never `platform` (shadows the stdlib).

The server must be outside Iran (Telegram, OpenRouter and certificate issuance all need direct egress).

## Setup

1. **Platform bot** in @BotFather: create a dedicated bot (never one another process already polls), then in its settings turn on **Secretary Mode**. Put its token and username in `hosting.env`.
2. **Login**: in BotFather switch the bot to OpenID login, copy the client id and client secret, and add the allowed redirect URL `https://<domain>/auth/callback`. Without a client id, `/login` falls back to the classic Login Widget (run `/setdomain` for the bot).
3. **Model provider**: set `ANTHROPIC_API_KEY` to run every tenant on Claude (`ANTHROPIC_MODEL`, default `claude-haiku-5-5`). One shared key goes into each tenant `.env`; each tenant records its own spend (`llm_usage` in its DB) and the platform writes `CREDIT_LIMIT_USD` = sum of payments into the tenant `.env` (restarting a running tenant) so the bot stops at zero. Voice notes are off on this provider and the UI says "coming soon". Alternative: leave it empty and set `OPENROUTER_MGMT_KEY` (a provisioning key from openrouter.ai/settings/provisioning-keys); the platform then mints one capped OpenRouter key per tenant.
4. **Config**: `cp hosting.env.example hosting.env` and fill it in (`PUBLIC_URL`, `ADMIN_TG_ID`, client id/secret, keys).
5. **Install**: `uv sync`
6. **Run**: `pm2 start hosting/ecosystem.config.cjs && pm2 save` (first time also `pm2 startup systemd -u $USER --hp $HOME`).
7. **Reverse proxy**: Caddy (`hosting/Caddyfile.example`) or an nginx server block proxying to `127.0.0.1:8700`, with HTTPS.

## Admin

Sign in with the Telegram account matching `ADMIN_TG_ID`, open `/admin`. After a user pays, record the amount there: it raises their OpenRouter key limit and starts their tenant. The same page can restart or stop a tenant.

## Tenant data

Each tenant lives in `tenants/<id>/` (its `.env`, `secretary.db`, prompts). The control plane state is `hosting.db`. Back both up.

Message text inside a tenant DB is pruned after `MESSAGE_RETENTION_DAYS` (default 7, user-adjustable down to 1 in their dashboard); only summaries and memory facts persist. `/privacy` on the site states this, says plainly that the operator can technically read tenant files, and links the self-host option. Nothing in the control plane reads message contents — keep it that way, the page promises it.

## Deleting a user

The user can delete their account from the account page (`/account`). That stops and removes the pm2 app `tgs-<id>`, deletes `tenants/<id>/`, disables the OpenRouter key and releases the tenant slot. The user should also remove the bot under Chat Automation; until then the router simply drops their updates.
