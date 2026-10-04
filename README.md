# tg-secretary

A personal Telegram autoresponder that answers your private chats as you while you're away. It connects to your account through Telegram Business chatbots, waits to see whether you reply yourself, and if you don't, writes a reply in your voice with an LLM via [OpenRouter](https://openrouter.ai). Each contact gets a relationship-specific persona (partner, best friend, family, work, stranger, ...), per-contact notes, and a memory that builds up over time. Sensitive messages can be held for your approval instead of being sent. It is built for a single owner. It is not a customer-support bot.

## Features

- Replies through your Telegram account using Business Mode. Contacts see your name and avatar.
- Backs off when you're active: you get a delay window to answer yourself, a cooldown after you type in a chat, quiet hours, and global or per-chat pause.
- Personas per relationship (`gf`, `bff`, `close_friend`, `friend`, `family`, `work`, `acquaintance`, `unknown`), stacked with per-contact prompt files and DB notes.
- Per-contact memory: a background worker pulls facts, preferences, events and inside jokes out of conversations, keeps rolling chat summaries, and builds a style fingerprint of how you write.
- Human-in-the-loop: approval mode sends every draft to you first. The inner-circle gate holds emotional, long or after-silence messages from partner, family and best friends. Drafts arrive in your bot DM with Send / Edit / Skip buttons.
- Voice notes are transcribed with Whisper (via OpenRouter) and answered like text.
- Bilingual Persian/English tuning: colloquial Persian spelling, language mirroring, and cleanup of machine-sounding output such as trailing periods, markdown and translation glosses.
- An LLM-judged prompt test suite and a local web tester for A/B/C model comparison.

## How it works

For each incoming text message:

1. Messages you type yourself are stored and never answered.
2. Global pause, per-chat pause and quiet hours are checked.
3. If you typed in this chat recently (owner-active cooldown), the bot stays silent.
4. It waits for the reply delay: 30s by default, 5s in "away mode" (the bot replied last and you didn't step in).
5. It checks again whether you typed during the wait, and aborts if you did.
6. Inner-circle gate: for `gf`/`bff`/`family`, an emotional keyword, a message over 200 chars, or more than 24h of silence turns the reply into a draft for you instead.
7. Approval mode, if on, turns every reply into a draft.
8. Otherwise it builds the system prompt (default persona, about-me, relationship persona, contact file, DB note, memory, style fingerprint), calls the model, checks once more that you haven't typed, then sends the reply and queues memory extraction.

Voice notes are transcribed first and then take the same path. Voice notes over 120s, or any voice note while transcription is off, notify you instead of getting an auto-reply.

## Requirements

- Python 3.13 and [uv](https://docs.astral.sh/uv/)
- A Telegram account. Since Bot API 10.0, business chatbots work without Telegram Premium.
- A Telegram bot token from [@BotFather](https://t.me/BotFather)
- An [OpenRouter](https://openrouter.ai/keys) API key

## Setup

1. Create the bot. In [@BotFather](https://t.me/BotFather) run `/newbot` and save the token. Then go to `/mybots`, pick your bot, open **Bot Settings > Business Mode** and enable it.
2. Get an [OpenRouter](https://openrouter.ai/keys) API key.
3. Install and run the setup wizard:
   ```bash
   git clone https://github.com/sepehr071/tg-secretary.git
   cd tg-secretary
   uv sync
   uv run python -m secretary.setup
   ```
   The wizard checks the bot token (and that Business Mode is on) and the API key, proves you own the bot by having you tap a one-time `/start` link, and writes `.env`. Re-running it is safe: Enter keeps each current value and the old `.env` is backed up. On Ubuntu, `./scripts/setup-ubuntu.sh` installs everything and runs the wizard for you.
4. Start it: `pm2 start ecosystem.config.cjs`, or `uv run python -m secretary` without pm2.
5. Connect it to your account. In the Telegram app open **Settings > Business > Chatbots**, enter your bot's username, and give it the **reply to messages** and **read messages** rights. Choose which chats it may access.
6. Send `/help` to your bot in a direct message to see the owner commands. The bot DMs you when the business connection is added or removed, and warns you if required rights are missing.

New chats start out untagged and use the `unknown` persona. Tag your contacts with `/who` (`/senders` lists recent chat ids), or in the dashboard.

## Dashboard

The bot serves a web dashboard on `127.0.0.1:8780` (set `DASHBOARD_PORT` to change it, `DASHBOARD_ENABLED=false` to turn it off). Send `/dashboard` to your bot to get a one-time login link. When the bot runs on a server, open a tunnel from your computer first: `ssh -L 8780:127.0.0.1:8780 user@server`. The dashboard covers live settings, config, business-connection status, contacts (relationship, notes, prompt files, memory), persona prompts and the approval queue.

To skip the tunnel, set `DASHBOARD_HOST=0.0.0.0` and `DASHBOARD_PUBLIC_URL=http://<server-ip>:8780` (and open the port in your firewall). Be aware that over plain http the login link and session cookie travel unencrypted, so anyone on the path could take over the session and send messages as you. An https reverse proxy (Caddy, nginx) in front with `DASHBOARD_PUBLIC_URL=https://...` fixes that; the session cookie is then marked Secure.

## Prompt customization

Prompts live in `prompts/`. Real prompt files hold personal data and are gitignored. The repo ships `*.example.txt` templates.

- **Relationship personas:** copy `prompts/personas/<rel>.example.txt` to `prompts/personas/<rel>.txt` and edit it. If `<rel>.txt` is missing, the loader falls back to the example file. `{owner_first_name}` is replaced with `OWNER_FIRST_NAME`.
- **About you:** copy `prompts/about_me.example.txt` to `prompts/about_me.txt`. It is injected into every prompt as `## About me`.
- **Per contact:** create `prompts/contacts/<chat_id>.txt` (see `prompts/contacts/template-gf.txt.example`).
- **From Telegram:** `/prompt <chat_id> <fact>` appends a line to that contact's file. `/note <chat_id> <text>` stores a DB-backed note.
- After editing files by hand, send `/reload_prompts`.

Write example lines as an illustrative range ("examples of the register, vary your own phrasing"), not as fixed lists or "they say X, you say Y" tables. Models treat closed lists as lookup tables, and the replies come out repetitive.

## Owner commands

Send these to the bot in a direct message. Only `OWNER_USER_ID` can use them.

| Command | What it does |
|---|---|
| `/help` | Show the command list |
| `/pause`, `/resume` | Global mute toggle |
| `/status` | Current settings and counts |
| `/stats` | Reply counts (today / week) |
| `/last [N]` | Last N bot replies |
| `/undo <chat_id>` | Delete the bot's last auto-reply in that chat |
| `/pause_chat <chat_id>`, `/resume_chat <chat_id>` | Mute or unmute one chat |
| `/who <chat_id> [relationship] [nickname]` | Tag a contact (no relationship opens an inline picker) |
| `/note <chat_id> <text>` | Append a DB-backed persona note |
| `/forget_chat <chat_id>` | Purge stored messages for a chat |
| `/persona_for <chat_id>` | Show the fully assembled system prompt |
| `/persona_path <chat_id>` | Path to the contact's `.txt` file |
| `/prompt <chat_id> [fact]` | Append a fact, or with no fact list facts with delete buttons |
| `/reload_prompts` | Re-read prompt files |
| `/memory <chat_id>` | List memory rows |
| `/remember <chat_id> <kind> <text>` | Add a memory row manually |
| `/forget <memory_id>` | Soft-delete a memory row |
| `/extract <chat_id>` | Run memory extraction now |
| `/style <chat_id>` | Show the style fingerprint |
| `/memory_cleanup <chat_id>\|all` | Deduplicate memory rows |
| `/memory_purge <chat_id>\|all` | Wipe memory and rebuild from scratch |
| `/approval on\|off` | Every reply becomes a draft for approval |
| `/innercircle on\|off` | Safety gate for partner / family / best friends |
| `/voice on\|off` | Voice transcription toggle |
| `/quiet HH:MM HH:MM\|off` | Quiet hours |
| `/delay [seconds]` | Auto-reply delay (default 30) |
| `/away_delay [seconds]` | Delay in away mode (default 5) |
| `/cooldown [seconds]` | Owner-active mute window (default 600) |
| `/preview <text>` | Dry-run a draft without sending |
| `/contacts` | List tagged chats |
| `/senders [N]` | Last N chats that messaged you, including untagged |
| `/find <query>` | Search nicknames and connections |
| `/say <chat_id> <text>` | Send a message as you, manually |
| `/pending` | List outstanding drafts |
| `/approve_<id>`, `/edit_<id> <text>`, `/skip_<id>` | Act on a draft (the Send / Edit / Skip buttons on the draft do the same) |
| `/backup` | Consistent snapshot of the database (SQLite online backup) to a timestamped file |

## Testing

There is no pytest suite. An offline smoke check covers the reply-pipeline logic (no network, no `.env`); prompts are tested with real OpenRouter calls graded by an LLM judge.

```bash
uv run python scripts/smoke_core.py                              # offline: debounce, draft claims, owner-only connections
uv run python scripts/test_prompts.py --no-judge                 # fast: assembly + reply + structural checks
uv run python scripts/test_prompts.py                            # full: adds judge grading per relationship rubric
uv run python scripts/test_prompts.py --scenario example_gf_banter --verbose
```

Scenarios are in `tests/fixtures/scenarios/`, rubrics in `tests/fixtures/rubrics/`. Snapshots of the assembled prompt, reply and verdict go to `tests/snapshots/`. `scripts/dump_fixtures.py` turns your own `secretary.db` into scenarios, anonymized by default. With `--no-anonymize` it writes `raw_*.json` files, which are gitignored, but they are still sent to OpenRouter when you run the tests.

Web tester for replaying a real chat against up to three models side by side:

```bash
PYTHONIOENCODING=utf-8 uv run python -m webtest   # http://127.0.0.1:8765/
```

## Deploy

On Ubuntu with pm2:

```bash
./scripts/setup-ubuntu.sh          # interactive, re-runnable: installs uv, Python 3.13, Node, pm2, writes .env, starts the bot
pm2 start ecosystem.config.cjs     # day-to-day start
pm2 logs tg-secretary
pm2 startup systemd -u $USER --hp $HOME   # run the printed sudo line, then:
pm2 save
```

## Privacy & Telegram rules

Read this before connecting the bot to real chats.

- **Contact messages go to third parties.** Message text, chat history, memory, and transcribed voice notes from your contacts are sent to OpenRouter and the model providers behind it. The [Telegram Bot Developer Terms](https://telegram.org/tos/bot-developers) (§5.4(iv)) require the user's authorization before business message contents are shared with third-party APIs. Make sure you have that authorization, and pick models and providers whose data policies you accept.
- **The database is plaintext.** `secretary.db` stores messages, memory and summaries unencrypted. The Bot Developer Terms (§4.4(a)) ask for data to be encrypted at rest. Run the bot on an encrypted disk (LUKS, BitLocker, FileVault) and restrict access to the host.
- **No spam.** Use it only to answer people who write to you. Don't use it for bulk or unsolicited messaging.
- **It speaks as you.** Contacts see your account, not a bot. You are responsible for everything it sends. Use approval mode or the inner-circle gate for the conversations that matter.
- **AI disclosure.** Some jurisdictions require telling people when they are talking to an AI. Check the laws that apply to you and your contacts.

## Security

The bot only serves the Business connection of `OWNER_USER_ID`; anyone else who attaches it to their account is ignored. `.env` contains the bot token and API key. Anyone who has it can read and send messages as you in every connected chat. Never commit it or paste it anywhere. If it leaks, revoke the token with [@BotFather](https://t.me/BotFather) (`/revoke`) and rotate the key in the OpenRouter dashboard. Telegram file download URLs include the bot token and can show up in HTTP debug logs, so treat logs as sensitive too.

## License

MIT. See [LICENSE](LICENSE).
