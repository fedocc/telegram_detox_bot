from __future__ import annotations

import os
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


def gmail_auth_settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        aitunnel_api_key="test",
        email_transport="gmail_api",
        gmail_oauth_client_secret_path=tmp_path / "google_oauth_client.json",
        gmail_oauth_token_path=tmp_path / "gmail_oauth_token.json",
        gmail_sender_email="sender@example.com",
        gmail_recipient_email="recipient@example.com",
    )


def run_verified_auth(settings: Settings) -> None:
    from app.cli.gmail_auth import run_gmail_auth

    run_gmail_auth(
        settings,
        flow_cls=FakeFlow,
        account_email_getter=lambda credentials: settings.gmail_sender_email,
    )


def test_missing_client_secret_fails_clearly(tmp_path) -> None:
    from app.cli.gmail_auth import run_gmail_auth

    settings = gmail_auth_settings(tmp_path)

    with pytest.raises(RuntimeError, match="Gmail OAuth client secret missing"):
        run_gmail_auth(settings, flow_cls=FakeFlow)


def test_gmail_auth_requests_send_and_email_identity_scopes(tmp_path) -> None:
    from app.cli.gmail_auth import GMAIL_OAUTH_SCOPES

    settings = gmail_auth_settings(tmp_path)
    settings.gmail_oauth_client_secret_path.write_text("{}", encoding="utf-8")

    run_verified_auth(settings)

    assert FakeFlow.scopes_seen == GMAIL_OAUTH_SCOPES
    assert "https://www.googleapis.com/auth/gmail.send" in FakeFlow.scopes_seen
    assert "https://www.googleapis.com/auth/userinfo.email" in FakeFlow.scopes_seen
    assert settings.gmail_oauth_token_path.stat().st_mode & 0o777 == 0o600


def test_gmail_auth_tells_user_to_log_in_as_sender_account(tmp_path, capsys) -> None:
    settings = gmail_auth_settings(tmp_path)
    settings.gmail_oauth_client_secret_path.write_text("{}", encoding="utf-8")

    run_verified_auth(settings)

    assert "Log in as the sender account." in capsys.readouterr().out


def test_gmail_auth_rejects_wrong_account_without_replacing_token(tmp_path) -> None:
    from app.cli.gmail_auth import run_gmail_auth

    settings = gmail_auth_settings(tmp_path)
    settings.gmail_oauth_client_secret_path.write_text("{}", encoding="utf-8")
    settings.gmail_oauth_token_path.write_text("old-token", encoding="utf-8")

    with pytest.raises(RuntimeError, match="does not match GMAIL_SENDER_EMAIL"):
        run_gmail_auth(
            settings,
            flow_cls=FakeFlow,
            account_email_getter=lambda credentials: "wrong@example.com",
        )

    assert settings.gmail_oauth_token_path.read_text(encoding="utf-8") == "old-token"
    assert not list(tmp_path.glob("gmail_oauth_token.json.bak*"))


def test_gmail_auth_backs_up_old_token_before_replacing_it(tmp_path) -> None:
    settings = gmail_auth_settings(tmp_path)
    settings.gmail_oauth_client_secret_path.write_text("{}", encoding="utf-8")
    settings.gmail_oauth_token_path.write_text("old-token", encoding="utf-8")

    run_verified_auth(settings)

    backups = list(tmp_path.glob("gmail_oauth_token.json.bak.*"))
    assert len(backups) == 1
    backup = backups[0]
    assert backup.read_text(encoding="utf-8") == "old-token"
    assert backup.stat().st_mode & 0o777 == 0o600
    assert settings.gmail_oauth_token_path.read_text(encoding="utf-8") != "old-token"


def test_gmail_auth_creates_unique_backups(tmp_path) -> None:
    settings = gmail_auth_settings(tmp_path)
    settings.gmail_oauth_client_secret_path.write_text("{}", encoding="utf-8")
    settings.gmail_oauth_token_path.write_text("first-token", encoding="utf-8")

    run_verified_auth(settings)
    run_verified_auth(settings)

    backups = list(tmp_path.glob("gmail_oauth_token.json.bak.*"))
    assert len(backups) == 2
    assert len({path.name for path in backups}) == 2
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in backups)


def test_backup_chmod_failure_aborts_before_token_replacement(tmp_path, monkeypatch) -> None:
    settings = gmail_auth_settings(tmp_path)
    settings.gmail_oauth_client_secret_path.write_text("{}", encoding="utf-8")
    settings.gmail_oauth_token_path.write_text("old-token", encoding="utf-8")
    real_chmod = os.chmod

    def fail_backup_chmod(path, mode):
        if ".bak." in Path(path).name:
            raise OSError("permission denied")
        real_chmod(path, mode)

    monkeypatch.setattr("app.email.sender.os.chmod", fail_backup_chmod)

    with pytest.raises(RuntimeError, match="permissions to 600") as exc_info:
        run_verified_auth(settings)

    assert settings.gmail_oauth_token_path.read_text(encoding="utf-8") == "old-token"
    assert not list(tmp_path.glob("gmail_oauth_token.json.bak.*"))
    assert "old-token" not in str(exc_info.value)


def test_new_token_chmod_failure_aborts_auth(tmp_path, monkeypatch) -> None:
    settings = gmail_auth_settings(tmp_path)
    settings.gmail_oauth_client_secret_path.write_text("{}", encoding="utf-8")
    settings.gmail_oauth_token_path.write_text("old-token", encoding="utf-8")
    real_chmod = os.chmod

    def fail_new_token_chmod(path, mode):
        name = Path(path).name
        if name.startswith(".gmail_oauth_token.json.") and name.endswith(".tmp"):
            raise OSError("permission denied")
        real_chmod(path, mode)

    monkeypatch.setattr("app.email.sender.os.chmod", fail_new_token_chmod)

    with pytest.raises(RuntimeError, match="permissions to 600") as exc_info:
        run_verified_auth(settings)

    assert settings.gmail_oauth_token_path.read_text(encoding="utf-8") == "old-token"
    assert len(list(tmp_path.glob("gmail_oauth_token.json.bak.*"))) == 1
    assert not list(tmp_path.glob("*.tmp"))
    assert "old-token" not in str(exc_info.value)


def test_final_token_mode_check_aborts_auth(tmp_path, monkeypatch) -> None:
    import app.cli.gmail_auth as gmail_auth

    settings = gmail_auth_settings(tmp_path)
    settings.gmail_oauth_client_secret_path.write_text("{}", encoding="utf-8")
    real_verify = gmail_auth._verify_mode_600

    def fail_final_token_check(path):
        if Path(path) == settings.gmail_oauth_token_path:
            raise RuntimeError("Credential file permissions must be 600.")
        real_verify(path)

    monkeypatch.setattr(gmail_auth, "_verify_mode_600", fail_final_token_check)

    with pytest.raises(RuntimeError, match="permissions must be 600"):
        run_verified_auth(settings)

    assert not settings.gmail_oauth_token_path.exists()


def test_final_backup_mode_check_aborts_before_token_replacement(
    tmp_path,
    monkeypatch,
) -> None:
    import app.cli.gmail_auth as gmail_auth

    settings = gmail_auth_settings(tmp_path)
    settings.gmail_oauth_client_secret_path.write_text("{}", encoding="utf-8")
    settings.gmail_oauth_token_path.write_text("old-token", encoding="utf-8")
    real_verify = gmail_auth._verify_mode_600

    def fail_backup_mode_check(path):
        if ".bak." in Path(path).name:
            raise RuntimeError("Credential file permissions must be 600.")
        real_verify(path)

    monkeypatch.setattr(gmail_auth, "_verify_mode_600", fail_backup_mode_check)

    with pytest.raises(RuntimeError, match="permissions must be 600") as exc_info:
        run_verified_auth(settings)

    assert settings.gmail_oauth_token_path.read_text(encoding="utf-8") == "old-token"
    assert not list(tmp_path.glob("gmail_oauth_token.json.bak.*"))
    assert "old-token" not in str(exc_info.value)


def test_successful_auth_leaves_token_and_backup_mode_600(tmp_path) -> None:
    settings = gmail_auth_settings(tmp_path)
    settings.gmail_oauth_client_secret_path.write_text("{}", encoding="utf-8")
    settings.gmail_oauth_token_path.write_text("old-token", encoding="utf-8")

    run_verified_auth(settings)

    backup = next(tmp_path.glob("gmail_oauth_token.json.bak.*"))
    assert settings.gmail_oauth_token_path.stat().st_mode & 0o777 == 0o600
    assert backup.stat().st_mode & 0o777 == 0o600


def test_success_message_requires_account_check_and_test_send() -> None:
    from app.cli.gmail_auth import SUCCESS_MESSAGE

    assert "test_email --account-check" in SUCCESS_MESSAGE
    assert "test_email" in SUCCESS_MESSAGE
