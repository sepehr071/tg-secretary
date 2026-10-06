from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class HostingSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file="hosting.env", env_file_encoding="utf-8", extra="ignore")

    platform_bot_token: str
    platform_bot_username: str
    # Empty = log in with the classic Login Widget (needs /setdomain on the platform bot).
    oidc_client_id: str = ""
    oidc_client_secret: str = ""
    openrouter_mgmt_key: str
    public_url: str  # https://example.com, no trailing slash
    admin_tg_id: int
    db_path: Path = Path("./hosting.db")
    tenants_root: Path = Path("./tenants")
    repo_root: Path = Path(__file__).resolve().parent.parent
    listen_port: int = 8700
    port_range_start: int = 9100
    port_range_end: int = 9199
    trial_credit_usd: float = 0.0
    consent_version: int = 1
    payment_instructions: str = ""  # shown on the awaiting-credit page (card number, wallet)


settings = HostingSettings()  # type: ignore[call-arg]
