# Secretary Bot

Personal Telegram **Secretary Bot**. Connects to your account via Telegram Business / Secretary Mode and auto-replies to incoming DMs using any LLM available through OpenRouter. Customers see your name + avatar, not the bot.

## Setup

1. Install [uv](https://docs.astral.sh/uv/) (already present on this machine).
2. Create the bot:
   - Talk to [@BotFather](https://t.me/BotFather) → `/newbot` → save the token.
   - `@BotFather` → `/mybots` → pick the bot → **Bot Settings → Business Mode → Enable**.
3. Connect the bot to your account:
   - Telegram app → **Settings → Business → Chatbots** → enter your bot's username.
   - Grant the rights you want (Reply, Read, etc.).
4. Get your Telegram numeric user id from [@userinfobot](https://t.me/userinfobot).
5. Configure:
   ```bash
   cd C:/Users/sepito/secretary-bot
   cp .env.example .env
   # edit .env: TG_BOT_TOKEN, OPENROUTER_API_KEY, OWNER_USER_ID, OWNER_FIRST_NAME
   ```
6. Install deps and run:
   ```bash
   uv sync
   uv run python -m secretary
   ```

## What it does

- Receives every DM sent to your personal account (only for the chats you've allowed).
- Skips messages you type yourself (owner-id guard).
- Loads the last N turns of history with that contact from SQLite.
- Calls OpenRouter (`anthropic/claude-sonnet-4-6` by default) with your persona prompt + history.
- Sends the reply back **as you** (the customer sees no bot — only a small indicator on your end).
- Persists every turn to `secretary.db`.

## Configuration

All settings live in `.env`. See `.env.example` for the full list.

To customise the persona, either:
- Edit `secretary/prompts.py` directly, or
- Point `SYSTEM_PROMPT_PATH` at a `.txt` file containing your prompt.

## Switching models

Change `OPENROUTER_MODEL` in `.env`. Any [OpenRouter model id](https://openrouter.ai/models) works.

## Disabling

In Telegram: **Settings → Business → Chatbots → disconnect**. The bot keeps running but stops receiving updates for your account. To stop the process: `Ctrl+C`.

## Files

```
secretary-bot/
├── pyproject.toml
├── .env / .env.example
├── secretary/
│   ├── __main__.py     # entrypoint
│   ├── config.py       # env loading
│   ├── db.py           # SQLite history store
│   ├── llm.py          # OpenRouter client (OpenAI SDK)
│   ├── prompts.py      # persona system prompt
│   └── handlers.py     # business connection + message handlers
└── secretary.db        # created on first run
```
