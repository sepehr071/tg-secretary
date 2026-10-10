from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    tg_bot_token: str
    openrouter_api_key: str
    openrouter_model: str = "google/gemini-3.8-flash"
    owner_user_id: int
    owner_first_name: str = "the owner"
    db_path: Path = Path("./secretary.db")
    history_turns: int = 12
    # Raw message rows older than this are rolled into the chat summary and deleted
    # (0 = keep forever). Live override: bot_state "retention_days".
    message_retention_days: int = 7
    system_prompt_path: Path | None = None

    # Wait this many seconds after a friend's message before replying.
    # If you start typing in that window, your manually-sent reply cancels the bot.
    auto_reply_delay_seconds: int = 30

    # Short delay used once "away mode" is detected in a chat
    # (you didn't preempt the bot's previous reply -> assumed away).
    # Resets to auto_reply_delay_seconds the moment you type manually in that chat.
    away_reply_delay_seconds: int = 5

    # If you sent anything (manually) in a chat within this many seconds,
    # the bot stays silent for that chat. Resets every time you type.
    owner_active_cooldown_seconds: int = 600

    voice_transcribe: bool = True
    whisper_model: str = "openai/whisper-large-v3"
    extractor_model: str = "google/gemini-3.1-flash-lite"
    prompts_dir: Path = Path("./prompts")

    # Owner web dashboard, served by the bot. Default: 127.0.0.1 only (reach it over `ssh -L`).
    dashboard_enabled: bool = True
    dashboard_port: int = 8780
    # Opt-in public access: DASHBOARD_HOST=0.0.0.0 plus the URL people open, e.g.
    # http://203.0.113.5:8780. Plain http exposes the session to anyone on the path;
    # prefer an https reverse proxy (the cookie turns Secure for https URLs).
    dashboard_host: str = "127.0.0.1"
    dashboard_public_url: str = ""
    # Tunnel command /dashboard shows; the setup wizard fills it when run over SSH.
    dashboard_ssh_hint: str = ""
    # Hosted platform mode: the dashboard sits behind the hosting proxy, which sends
    # X-Platform-Auth; token/key/owner fields are hidden because the platform owns them.
    hosted: bool = False
    dashboard_proxy_secret: str = ""
    # URL prefix the proxy serves the dashboard under, e.g. "/app". Empty = served at /.
    dashboard_root_path: str = ""
    dashboard_lang: str = "en"


settings = Settings()  # type: ignore[call-arg]
