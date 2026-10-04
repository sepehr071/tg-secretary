from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    tg_bot_token: str
    openrouter_api_key: str
    openrouter_model: str = "openai/gpt-5.5"
    owner_user_id: int
    owner_first_name: str = "the owner"
    db_path: Path = Path("./secretary.db")
    history_turns: int = 12
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


settings = Settings()  # type: ignore[call-arg]
