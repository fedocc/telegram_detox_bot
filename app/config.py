from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    aitunnel_base_url: str = "https://api.aitunnel.ru/v1/"
    aitunnel_model: str = "claude-haiku-4.5"
    aitunnel_api_key: str = ""

    email_transport: str = "gmail_api"
    gmail_oauth_client_secret_path: Path = Path("secrets/google_oauth_client.json")
    gmail_oauth_token_path: Path = Path("data/gmail_oauth_token.json")
    gmail_sender_email: str = "fnikonov999@gmail.com"
    gmail_sender_name: str = ""
    gmail_recipient_email: str = ""

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
    telegram_use_ipv6: bool = False

    database_url: str = "sqlite:///data/telegram_digest.db"
    timezone: str = "Europe/Moscow"
    digest_enabled: bool = True
    digest_time: str = "07:00"
    digest_model: str = "gemini-3.8-flash"
    raw_retention_days: int = Field(default=14, ge=1)
    digest_retention_days: int = Field(default=90, ge=1)
    birthday_reminders_enabled: bool = False
    birthday_poll_interval_hours: int = Field(default=6, ge=1, le=24)
    birthday_reminder_time: str = "09:00"
    birthday_lookahead_days: int = Field(default=1, ge=0, le=1)
    birthday_manual_path: Path = Path("data/birthdays.json")
    ignore_chat_ids: str = ""
    ignored_chats_path: Path = Path("data/ignored_chats.json")
    library_chats_path: Path = Path("data/library_chats.json")
    inbox_upload_max_mb: int = Field(default=100, ge=1, le=2000)
    inbox_upload_concurrency: int = Field(default=2, ge=1, le=8)
    inbox_upload_stale_hours: int = Field(default=24, ge=1, le=168)
    inbox_allowed_origins: str = "http://127.0.0.1:8787"
    inbox_cloudflare_origin: str = ""
    inbox_library_management_enabled: bool = False
    web_push_vapid_private_key: str = ""
    web_push_vapid_subject: str = "mailto:fnikonov999@gmail.com"
    inbox_enabled: bool = True
    mention_only_mode: bool = False
    p0_classify_private_text: bool = True
    p0_classify_all_groups: bool = False
    p0_classify_mentions: bool = True
    p0_classify_replies: bool = True
    p0_classify_watchlist_chats: bool = True
    p0_watchlist_chat_ids: str = ""
    p0_watchlist_keywords: str = ""
    p0_mention_usernames: str = "fedocc,me,fedornikonov"
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

    @field_validator("inbox_allowed_origins")
    @classmethod
    def validate_inbox_allowed_origins(cls, value: str) -> str:
        origins = []
        for raw in value.split(","):
            origin = raw.strip().rstrip("/")
            if not origin or "*" in origin:
                raise ValueError("INBOX_ALLOWED_ORIGINS must contain exact origins")
            parsed = urlsplit(origin)
            if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                    or parsed.username or parsed.password or parsed.path
                    or parsed.query or parsed.fragment):
                raise ValueError("INBOX_ALLOWED_ORIGINS must contain exact origins")
            if parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "localhost"}:
                raise ValueError("HTTP inbox origins must be loopback")
            origins.append(origin)
        if not origins or len(origins) != len(set(origins)):
            raise ValueError("INBOX_ALLOWED_ORIGINS must contain unique exact origins")
        return ",".join(origins)

    @field_validator("gmail_sender_name")
    @classmethod
    def validate_gmail_sender_name(cls, value: str) -> str:
        if "\r" in value or "\n" in value:
            raise ValueError("GMAIL_SENDER_NAME must not contain newlines")
        return value.strip()

    @field_validator("birthday_reminder_time")
    @classmethod
    def validate_birthday_reminder_time(cls, value: str) -> str:
        parts = value.strip().split(":")
        if len(parts) != 2 or not all(part.isdigit() for part in parts):
            raise ValueError("BIRTHDAY_REMINDER_TIME must use HH:MM")
        hour, minute = (int(part) for part in parts)
        if not 0 <= hour <= 23 or not 0 <= minute <= 59:
            raise ValueError("BIRTHDAY_REMINDER_TIME must use HH:MM")
        return f"{hour:02d}:{minute:02d}"

    @model_validator(mode="after")
    def preserve_legacy_gmail_recipient(self):
        # Existing deployments used EMAIL_TO for both Gmail API and SMTP.
        if not self.gmail_recipient_email and self.email_to:
            self.gmail_recipient_email = self.email_to
        return self

    @model_validator(mode="after")
    def validate_cloudflare_origin(self):
        cloudflare = self.inbox_cloudflare_origin.strip().rstrip("/")
        if cloudflare:
            parsed = urlsplit(cloudflare)
            if (parsed.scheme != "https" or not parsed.hostname or parsed.username
                    or parsed.password or parsed.path or parsed.query or parsed.fragment
                    or "*" in cloudflare or parsed.hostname.endswith(".trycloudflare.com")):
                raise ValueError("INBOX_CLOUDFLARE_ORIGIN must be an exact custom HTTPS origin")
        for origin in self.allowed_inbox_origins:
            parsed = urlsplit(origin)
            if (parsed.scheme == "https" and not parsed.hostname.endswith(".ts.net")
                    and origin != cloudflare):
                raise ValueError(
                    "Public HTTPS inbox origin must equal INBOX_CLOUDFLARE_ORIGIN"
                )
        if cloudflare and cloudflare not in self.allowed_inbox_origins:
            raise ValueError("INBOX_CLOUDFLARE_ORIGIN must be included in INBOX_ALLOWED_ORIGINS")
        self.inbox_cloudflare_origin = cloudflare
        return self

    def ensure_runtime_dirs(self) -> None:
        Path("data").mkdir(mode=0o700, exist_ok=True)
        Path("logs").mkdir(mode=0o700, exist_ok=True)

    @property
    def allowed_inbox_origins(self) -> tuple[str, ...]:
        return tuple(self.inbox_allowed_origins.split(","))

    def require_telegram_credentials(self) -> None:
        if self.tg_api_id is None or not self.tg_api_hash or not self.tg_phone:
            raise RuntimeError("TG_API_ID, TG_API_HASH, and TG_PHONE are required")


def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_runtime_dirs()
    return settings
