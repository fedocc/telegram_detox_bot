from __future__ import annotations

import getpass
import os
from pathlib import Path

FIELDS = [
    ("AITUNNEL_API_KEY", True, ""),
    ("SMTP_USERNAME", False, ""),
    ("SMTP_PASSWORD", True, ""),
    ("EMAIL_FROM", False, ""),
    ("EMAIL_TO", False, ""),
    ("TG_API_ID", False, ""),
    ("TG_API_HASH", True, ""),
    ("TG_PHONE", False, ""),
    ("TIMEZONE", False, "Europe/Moscow"),
    ("DIGEST_TIME", False, "20:30"),
]


def main() -> None:
    values = {
        "AITUNNEL_BASE_URL": "https://api.aitunnel.ru/v1/",
        "AITUNNEL_MODEL": "claude-haiku-4.5",
        "SMTP_HOST": "smtp.gmail.com",
        "SMTP_PORT": "465",
        "DATABASE_URL": "sqlite:///data/telegram_digest.db",
        "TG_SESSION_PATH": "data/telegram_digest.session",
    }
    for name, secret, default in FIELDS:
        prompt = f"{name}" + (f" [{default}]" if default else "") + ": "
        value = getpass.getpass(prompt) if secret else input(prompt)
        values[name] = value or default
    path = Path(".env")
    lines = [f"{key}={value}" for key, value in values.items()]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)
    print("Created .env with variables:")
    for key in values:
        print(f"- {key}")


if __name__ == "__main__":
    main()

