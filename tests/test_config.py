from __future__ import annotations

import pytest

from app.config import Settings
from app.telegram.client import make_client


def test_empty_tg_api_id_is_treated_as_none() -> None:
    settings = Settings(_env_file=None, tg_api_id="", tg_api_hash="", tg_phone="")

    assert settings.tg_api_id is None
    assert settings.tg_api_hash is None
    assert settings.tg_phone is None


def test_missing_telegram_fields_allow_test_llm_config_load() -> None:
    settings = Settings(
        _env_file=None,
        aitunnel_api_key="test-key",
        tg_api_id="",
        tg_api_hash="",
        tg_phone="",
    )

    assert settings.aitunnel_api_key == "test-key"
    assert settings.tg_api_id is None


def test_telegram_login_rejects_missing_credentials_with_clear_error() -> None:
    settings = Settings(_env_file=None, tg_api_id=None, tg_api_hash=None, tg_phone=None)

    with pytest.raises(RuntimeError, match="TG_API_ID, TG_API_HASH, and TG_PHONE are required"):
        make_client(settings)


def test_birthday_scheduler_defaults(monkeypatch) -> None:
    for variable in (
        "BIRTHDAY_REMINDERS_ENABLED",
        "BIRTHDAY_POLL_INTERVAL_HOURS",
        "BIRTHDAY_REMINDER_TIME",
        "BIRTHDAY_LOOKAHEAD_DAYS",
    ):
        monkeypatch.delenv(variable, raising=False)
    settings = Settings(_env_file=None)

    assert settings.birthday_reminders_enabled is False
    assert settings.birthday_poll_interval_hours == 6
    assert settings.birthday_reminder_time == "09:00"
    assert settings.birthday_lookahead_days == 1


def test_birthday_defaults_ignore_real_cwd_env_file(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("BIRTHDAY_REMINDERS_ENABLED", raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        "BIRTHDAY_REMINDERS_ENABLED=true\n",
        encoding="utf-8",
    )

    assert Settings().birthday_reminders_enabled is True
    assert Settings(_env_file=None).birthday_reminders_enabled is False


def test_birthday_reminder_time_is_validated() -> None:
    with pytest.raises(ValueError, match="BIRTHDAY_REMINDER_TIME"):
        Settings(_env_file=None, birthday_reminder_time="25:00")
