from __future__ import annotations

import getpass
import os
from pathlib import Path

FIELDS = [
    ("AITUNNEL_API_KEY", True, ""),
    ("TG_API_ID", False, ""),
    ("TG_API_HASH", True, ""),
    ("TG_PHONE", False, ""),
    ("TIMEZONE", False, "Europe/Moscow"),
    ("DIGEST_TIME", False, "20:30"),
]
SMTP_FIELDS = [
    ("SMTP_USERNAME", False, ""),
    ("SMTP_PASSWORD", True, ""),
    ("SMTP_TLS_MODE", False, "ssl"),
]
EMAIL_FIELDS = [
    ("EMAIL_FROM", False, ""),
    ("EMAIL_TO", False, ""),
]
GMAIL_EMAIL_FIELDS = [
    ("GMAIL_SENDER_EMAIL", False, "fnikonov999@gmail.com"),
    ("GMAIL_SENDER_NAME", False, "TELEGRAM"),
    ("GMAIL_RECIPIENT_EMAIL", False, ""),
]


def quote_dotenv_value(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{escaped}"'


def format_env(values: dict[str, str]) -> str:
    return "\n".join(f"{key}={quote_dotenv_value(value)}" for key, value in values.items()) + "\n"


def main() -> None:
    email_transport = input("Email transport [gmail_api]: ") or "gmail_api"
    values = {
        "AITUNNEL_BASE_URL": "https://api.aitunnel.ru/v1/",
        "AITUNNEL_MODEL": "claude-haiku-4.5",
        "EMAIL_TRANSPORT": email_transport,
        "SMTP_HOST": "smtp.gmail.com",
        "SMTP_PORT": "465",
        "DATABASE_URL": "sqlite:///data/telegram_digest.db",
        "TG_SESSION_PATH": "data/telegram_digest.session",
        "BIRTHDAY_REMINDERS_ENABLED": "false",
        "BIRTHDAY_POLL_INTERVAL_HOURS": "6",
        "BIRTHDAY_REMINDER_TIME": "09:00",
        "BIRTHDAY_LOOKAHEAD_DAYS": "1",
        "BIRTHDAY_MANUAL_PATH": "data/birthdays.json",
        "IGNORE_CHAT_IDS": "",
    }
    if email_transport == "gmail_api":
        values["GMAIL_OAUTH_CLIENT_SECRET_PATH"] = "secrets/google_oauth_client.json"  # noqa: S105
        values["GMAIL_OAUTH_TOKEN_PATH"] = "data/gmail_oauth_token.json"  # noqa: S105
        flow_fields = [*GMAIL_EMAIL_FIELDS, *FIELDS]
    else:
        flow_fields = [*SMTP_FIELDS, *EMAIL_FIELDS, *FIELDS]
    for name, secret, default in flow_fields:
        hint = " (ssl = port 465, starttls = port 587)" if name == "SMTP_TLS_MODE" else ""
        prompt = f"{name}{hint}" + (f" [{default}]" if default else "") + ": "
        value = getpass.getpass(prompt) if secret else input(prompt)
        values[name] = value or default
    path = Path(".env")
    path.write_text(format_env(values), encoding="utf-8")
    os.chmod(path, 0o600)
    print("Created .env with variables:")
    for key in values:
        print(f"- {key}")


if __name__ == "__main__":
    main()
