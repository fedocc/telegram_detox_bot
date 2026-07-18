from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from app.config import Settings
from app.db.session import init_db


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        aitunnel_api_key="test-key",
        smtp_username="user",
        smtp_password="",
        email_from="from@example.com",
        email_to="to@example.com",
    )


@pytest.fixture()
def session(settings: Settings):
    factory = init_db(settings)
    with factory() as db:
        yield db


@pytest.fixture()
def now() -> datetime:
    return datetime.fromisoformat("2026-07-07T12:00:00+03:00")
