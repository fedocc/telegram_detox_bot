from __future__ import annotations

from dotenv import dotenv_values

from app.cli.setup_env import GMAIL_EMAIL_FIELDS, format_env


def test_setup_env_quotes_special_characters_safely(tmp_path) -> None:
    path = tmp_path / ".env"
    values = {
        "AITUNNEL_API_KEY": 'abc$ # "quote" \\ slash',
        "SMTP_PASSWORD": "space value # hash",
        "TG_API_HASH": "back\\slash",
    }

    path.write_text(format_env(values), encoding="utf-8")
    parsed = dotenv_values(path)

    assert parsed["AITUNNEL_API_KEY"] == values["AITUNNEL_API_KEY"]
    assert parsed["SMTP_PASSWORD"] == values["SMTP_PASSWORD"]
    assert parsed["TG_API_HASH"] == values["TG_API_HASH"]


def test_makefile_uses_project_virtualenv() -> None:
    text = __import__("pathlib").Path("Makefile").read_text(encoding="utf-8")

    assert "PYTHON := .venv/bin/python" in text
    assert "$(PYTHON) -m pytest" in text
    assert "$(PYTHON) -m ruff check ." in text
    assert "Virtualenv missing. Run make setup." in text


def test_setup_env_uses_separate_gmail_sender_and_recipient_fields() -> None:
    names = [name for name, secret, default in GMAIL_EMAIL_FIELDS]

    assert names == [
        "GMAIL_SENDER_EMAIL",
        "GMAIL_SENDER_NAME",
        "GMAIL_RECIPIENT_EMAIL",
    ]
    assert GMAIL_EMAIL_FIELDS[0][2] == "fnikonov999@gmail.com"
    assert GMAIL_EMAIL_FIELDS[1][2] == "TELEGRAM"
