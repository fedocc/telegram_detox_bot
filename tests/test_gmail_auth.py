from __future__ import annotations

import inspect
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
        self.profile_called = False

    def users(self):
        return FakeUsers(self.email)


class NoProfileService:
    def users(self):
        raise AssertionError("getProfile must not be called with gmail.send-only scope")


def gmail_auth_settings(tmp_path: Path, email_from: str = "from@example.com") -> Settings:
    return Settings(
        _env_file=None,
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
        run_gmail_auth(settings, flow_cls=FakeFlow)


def test_gmail_auth_does_not_validate_profile_matches_email_from(tmp_path) -> None:
    from app.cli.gmail_auth import run_gmail_auth

    settings = gmail_auth_settings(tmp_path, email_from="from@example.com")
    settings.gmail_oauth_client_secret_path.write_text("{}", encoding="utf-8")

    run_gmail_auth(settings, flow_cls=FakeFlow)

    assert settings.gmail_oauth_token_path.exists()


def test_gmail_auth_does_not_call_get_profile(tmp_path) -> None:
    import app.cli.gmail_auth as gmail_auth
    from app.cli.gmail_auth import run_gmail_auth

    settings = gmail_auth_settings(tmp_path)
    settings.gmail_oauth_client_secret_path.write_text("{}", encoding="utf-8")

    run_gmail_auth(
        settings,
        flow_cls=FakeFlow,
    )

    assert settings.gmail_oauth_token_path.exists()
    assert "getProfile" not in inspect.getsource(gmail_auth.run_gmail_auth)


def test_gmail_auth_uses_only_gmail_send_scope(tmp_path) -> None:
    from app.cli.gmail_auth import GMAIL_SEND_SCOPES, run_gmail_auth

    settings = gmail_auth_settings(tmp_path)
    settings.gmail_oauth_client_secret_path.write_text("{}", encoding="utf-8")

    run_gmail_auth(
        settings,
        flow_cls=FakeFlow,
    )

    assert FakeFlow.scopes_seen == GMAIL_SEND_SCOPES
    assert FakeFlow.scopes_seen == ["https://www.googleapis.com/auth/gmail.send"]
    assert settings.gmail_oauth_token_path.stat().st_mode & 0o777 == 0o600


def test_gmail_auth_saves_token_without_profile_scope(tmp_path) -> None:
    from app.cli.gmail_auth import run_gmail_auth

    settings = gmail_auth_settings(tmp_path)
    settings.gmail_oauth_client_secret_path.write_text("{}", encoding="utf-8")

    run_gmail_auth(
        settings,
        flow_cls=FakeFlow,
    )

    assert settings.gmail_oauth_token_path.read_text(encoding="utf-8")
    assert settings.gmail_oauth_token_path.stat().st_mode & 0o777 == 0o600


def test_test_email_is_required_to_verify_sending() -> None:
    from app.cli.gmail_auth import SUCCESS_MESSAGE

    assert "Run test_email to verify sending account" in SUCCESS_MESSAGE
