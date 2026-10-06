# Hosting platform

Control plane that lets users sign in with Telegram, get their own managed secretary bot, and run it as a per-tenant pm2 process (`tgs-<id>`). Package name is `hosting`, never `platform` (shadows the stdlib).

The server must be outside Iran (Telegram, OpenRouter and Caddy certificates all need direct egress).

## Setup

1. **Platform bot** in @BotFather: create a bot, then in its settings turn on **Bot Management Mode**. Put its token and username in `hosting.env`.
2. **Login Widget**: in BotFather open the bot's Login Widget / Web Login settings, copy the client id and client secret, and add the allowed redirect URL `https://<domain>/auth/callback`.
3. **OpenRouter**: create a management (provisioning) key at openrouter.ai/settings/provisioning-keys. It mints one capped key per tenant.
4. **Config**: `cp hosting.env.example hosting.env` and fill it in (`PUBLIC_URL`, `ADMIN_TG_ID`, client id/secret, keys).
5. **Install**: `uv sync`
6. **Run**: `pm2 start hosting/ecosystem.config.cjs && pm2 save` (first time also `pm2 startup systemd -u $USER --hp $HOME`).
7. **Caddy**: copy `hosting/Caddyfile.example` into your Caddyfile, set your domain, reload Caddy. It proxies to `127.0.0.1:8700` and handles HTTPS.

If the onboarding page says Secretary Mode is off, the user enables it for their account in BotFather and clicks "check again".

## Admin

Sign in with the Telegram account matching `ADMIN_TG_ID`, open `/admin`. After a user pays, record the amount there: it raises their OpenRouter key limit and starts their tenant. The same page can restart or stop a tenant.

## Tenant data

Each tenant lives in `tenants/<id>/` (its `.env`, `secretary.db`, prompts). The control plane state is `hosting.db`. Back both up.

## Deleting a user

The user can delete their account from the account page (`/account`). That stops and removes the pm2 app `tgs-<id>`, deletes `tenants/<id>/`, disables the OpenRouter key, revokes the managed bot's old token and releases the tenant slot.
