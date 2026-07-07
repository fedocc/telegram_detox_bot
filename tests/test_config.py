from __future__ import annotations

import pytest

from app.config import Settings
from app.telegram.client import make_client


def test_empty_tg_api_id_is_treated_as_none() -> None:
    settings = Settings(tg_api_id="", tg_api_hash="", tg_phone="")

    assert settings.tg_api_id is None
    assert settings.tg_api_hash is None
    assert settings.tg_phone is None


def test_missing_telegram_fields_allow_test_llm_config_load() -> None:
    settings = Settings(aitunnel_api_key="test-key", tg_api_id="", tg_api_hash="", tg_phone="")

    assert settings.aitunnel_api_key == "test-key"
    assert settings.tg_api_id is None


def test_telegram_login_rejects_missing_credentials_with_clear_error() -> None:
    settings = Settings(tg_api_id=None, tg_api_hash=None, tg_phone=None)

    with pytest.raises(RuntimeError, match="TG_API_ID, TG_API_HASH, and TG_PHONE are required"):
        make_client(settings)

