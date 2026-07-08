from __future__ import annotations

from pathlib import Path

import pytest

from app.config import Settings


class FakeCredentials:
    def to_json(self) -> str:
        return '{"token":"access","refresh_token":"refresh"}'


class FakeFlow:
    scopes_seen = None

    @classmethod
    def from_client_secrets_file(cls, path, scopes):
        cls.scopes_seen = scopes
        return cls()

    def run_local_server(self, port: int = 0):
        return FakeCredentials()


class FakeExecute:
    def __init__(self, email: str):
        self.email = email

    def execute(self):
        return {"emailAddress": self.email}


class FakeUsers:
    def __init__(self, email: str):
        self.email = email

    def getProfile(self, userId: str):
        return FakeExecute(self.email)


class FakeService:
    def __init__(self, email: str):
        self.email = email

    def users(self):
        return FakeUsers(self.email)


def gmail_auth_settings(tmp_path: Path, email_from: str = "from@example.com") -> Settings:
    return Settings(
        aitunnel_api_key="test",
        email_transport="gmail_api",
        gmail_oauth_client_secret_path=tmp_path / "google_oauth_client.json",
        gmail_oauth_token_path=tmp_path / "gmail_oauth_token.json",
        email_from=email_from,
        email_to="to@example.com",
    )


def test_missing_client_secret_fails_clearly(tmp_path) -> None:
    from app.cli.gmail_auth import run_gmail_auth

    settings = gmail_auth_settings(tmp_path)

    with pytest.raises(RuntimeError, match="Gmail OAuth client secret missing"):
        run_gmail_auth(settings, flow_cls=FakeFlow, build_service=lambda *args, **kwargs: None)


def test_gmail_auth_validates_profile_matches_email_from(tmp_path) -> None:
    from app.cli.gmail_auth import run_gmail_auth

    settings = gmail_auth_settings(tmp_path, email_from="from@example.com")
    settings.gmail_oauth_client_secret_path.write_text("{}", encoding="utf-8")

    with pytest.raises(RuntimeError, match="OAuth Gmail account mismatch"):
        run_gmail_auth(
            settings,
            flow_cls=FakeFlow,
            build_service=lambda *args, **kwargs: FakeService("other@example.com"),
        )

    assert settings.gmail_oauth_token_path.exists()


def test_gmail_auth_uses_only_gmail_send_scope(tmp_path) -> None:
    from app.cli.gmail_auth import GMAIL_SEND_SCOPES, run_gmail_auth

    settings = gmail_auth_settings(tmp_path)
    settings.gmail_oauth_client_secret_path.write_text("{}", encoding="utf-8")

    run_gmail_auth(
        settings,
        flow_cls=FakeFlow,
        build_service=lambda *args, **kwargs: FakeService("from@example.com"),
    )

    assert FakeFlow.scopes_seen == GMAIL_SEND_SCOPES
    assert FakeFlow.scopes_seen == ["https://www.googleapis.com/auth/gmail.send"]
    assert settings.gmail_oauth_token_path.stat().st_mode & 0o777 == 0o600
