from __future__ import annotations

from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    aitunnel_base_url: str = "https://api.aitunnel.ru/v1/"
    aitunnel_model: str = "claude-haiku-4.5"
    aitunnel_api_key: str = ""

    email_transport: str = "gmail_api"
    gmail_oauth_client_secret_path: Path = Path("secrets/google_oauth_client.json")
    gmail_oauth_token_path: Path = Path("data/gmail_oauth_token.json")

    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 465
    smtp_tls_mode: str = "ssl"
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
    p0_classify_private_text: bool = True
    p0_classify_all_groups: bool = False
    p0_classify_mentions: bool = True
    p0_classify_replies: bool = True
    p0_classify_watchlist_chats: bool = True
    p0_watchlist_chat_ids: str = ""
    p0_watchlist_keywords: str = ""
    p0_mention_usernames: str = "me,fedornikonov"
    p0_trusted_sender_ids: str = ""
    p0_max_context_messages: int = Field(default=5, ge=0, le=20)
    p0_max_message_chars: int = Field(default=1000, ge=100, le=5000)
    p0_max_llm_calls_per_hour: int = Field(default=100, ge=0)
    backfill_enabled: bool = True
    backfill_hours: int = Field(default=24, ge=1)
    backfill_max_messages_per_chat: int = Field(default=200, ge=1)
    backfill_max_total_messages: int = Field(default=5000, ge=1)
    p0_backfill_immediate_window_minutes: int = Field(default=30, ge=0)

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

    @field_validator("smtp_tls_mode")
    @classmethod
    def validate_smtp_tls_mode(cls, value: str) -> str:
        normalized = value.lower().strip()
        if normalized not in {"ssl", "starttls"}:
            raise ValueError("SMTP_TLS_MODE must be one of: ssl, starttls")
        return normalized

    @field_validator("email_transport")
    @classmethod
    def validate_email_transport(cls, value: str) -> str:
        normalized = value.lower().strip()
        if normalized not in {"gmail_api", "smtp"}:
            raise ValueError("EMAIL_TRANSPORT must be one of: gmail_api, smtp")
        return normalized

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
