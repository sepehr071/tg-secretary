# Hosting platform

Control plane that lets users sign in with Telegram, connect one shared secretary bot under Telegram Settings > Chat Automation, and runs each user's secretary as a per-tenant pm2 process (`tgs-<id>`). The control plane is the only process polling the shared bot; it forwards each update to the owning tenant over loopback. Package name is `hosting`, never `platform` (shadows the stdlib).

The server must be outside Iran (Telegram, OpenRouter and certificate issuance all need direct egress).

## Setup

1. **Platform bot** in @BotFather: create a dedicated bot (never one another process already polls), then in its settings turn on **Secretary Mode**. Put its token and username in `hosting.env`.
2. **Login**: in BotFather switch the bot to OpenID login, copy the client id and client secret, and add the allowed redirect URL `https://<domain>/auth/callback`. Without a client id, `/login` falls back to the classic Login Widget (run `/setdomain` for the bot).
3. **OpenRouter**: create a management (provisioning) key at openrouter.ai/settings/provisioning-keys. It mints one capped key per tenant.
4. **Config**: `cp hosting.env.example hosting.env` and fill it in (`PUBLIC_URL`, `ADMIN_TG_ID`, client id/secret, keys).
5. **Install**: `uv sync`
6. **Run**: `pm2 start hosting/ecosystem.config.cjs && pm2 save` (first time also `pm2 startup systemd -u $USER --hp $HOME`).
7. **Reverse proxy**: Caddy (`hosting/Caddyfile.example`) or an nginx server block proxying to `127.0.0.1:8700`, with HTTPS.

## Admin

Sign in with the Telegram account matching `ADMIN_TG_ID`, open `/admin`. After a user pays, record the amount there: it raises their OpenRouter key limit and starts their tenant. The same page can restart or stop a tenant.

## Tenant data

Each tenant lives in `tenants/<id>/` (its `.env`, `secretary.db`, prompts). The control plane state is `hosting.db`. Back both up.

## Deleting a user

The user can delete their account from the account page (`/account`). That stops and removes the pm2 app `tgs-<id>`, deletes `tenants/<id>/`, disables the OpenRouter key and releases the tenant slot. The user should also remove the bot under Chat Automation; until then the router simply drops their updates.
