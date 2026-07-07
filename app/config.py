from __future__ import annotations

from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    aitunnel_base_url: str = "https://api.aitunnel.ru/v1/"
    aitunnel_model: str = "claude-haiku-4.5"
    aitunnel_api_key: str = ""

    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 465
    smtp_username: str = ""
    smtp_password: str = ""
    email_from: str = ""
    email_to: str = ""

    tg_api_id: int | None = None
    tg_api_hash: str | None = None
    tg_phone: str | None = None
    tg_session_path: Path = Path("data/telegram_digest.session")

    database_url: str = "sqlite:///data/telegram_digest.db"
    timezone: str = "Europe/Moscow"
    digest_time: str = "20:30"
    raw_retention_days: int = Field(default=14, ge=1)
    digest_retention_days: int = Field(default=90, ge=1)

    @field_validator("tg_api_id", mode="before")
    @classmethod
    def empty_int_to_none(cls, value):
        if value == "":
            return None
        return value

    @field_validator("tg_api_hash", "tg_phone", mode="before")
    @classmethod
    def empty_str_to_none(cls, value):
        if value == "":
            return None
        return value

    def ensure_runtime_dirs(self) -> None:
        Path("data").mkdir(mode=0o700, exist_ok=True)
        Path("logs").mkdir(mode=0o700, exist_ok=True)

    def require_telegram_credentials(self) -> None:
        if self.tg_api_id is None or not self.tg_api_hash or not self.tg_phone:
            raise RuntimeError("TG_API_ID, TG_API_HASH, and TG_PHONE are required")


def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_runtime_dirs()
    return settings
