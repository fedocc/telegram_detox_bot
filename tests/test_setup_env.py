from __future__ import annotations

from dotenv import dotenv_values

from app.cli.setup_env import format_env


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
