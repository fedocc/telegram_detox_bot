from __future__ import annotations

import pytest

from app.config import Settings
from app.email.sender import EmailSendError


class FailingSender:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def send(self, subject: str, text: str, html: str | None = None, **kwargs) -> None:
        raise EmailSendError(
            "Gmail API send failed:\n"
            "status=403\n"
            "reason=insufficientPermissions\n"
            "message=Request had insufficient authentication scopes.\n"
            "hint=Delete Gmail OAuth token and rerun gmail_auth."
        )


class AccountCheckSender:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def account_status(self) -> tuple[str, str, bool]:
        return "sender@example.com", "recipient@example.com", True


class FailingAccountCheckSender:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def account_status(self) -> tuple[str, str, bool]:
        raise EmailSendError("provider access_token secret")


class CapturingSender:
    subject = ""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def send(self, subject: str, text: str, html: str | None = None, **kwargs) -> None:
        CapturingSender.subject = subject


def test_test_email_prints_safe_google_error(monkeypatch, capsys) -> None:
    import app.cli.test_email as cli

    settings = Settings(
        _env_file=None,
        aitunnel_api_key="test",
        email_transport="gmail_api",
        gmail_sender_email="sender@example.com",
        gmail_recipient_email="recipient@example.com",
    )
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr(cli, "EmailSender", FailingSender)

    with pytest.raises(SystemExit):
        cli.main([])

    captured = capsys.readouterr()
    assert "status=403" in captured.err
    assert "reason=insufficientPermissions" in captured.err
    assert "Request had insufficient authentication scopes." in captured.err
    assert "access_token" not in captured.err


def test_account_check_prints_only_safe_account_fields(monkeypatch, capsys) -> None:
    import app.cli.test_email as cli

    settings = Settings(
        _env_file=None,
        email_transport="gmail_api",
        gmail_sender_email="sender@example.com",
        gmail_recipient_email="recipient@example.com",
    )
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr(cli, "GmailApiSender", AccountCheckSender)

    cli.main(["--account-check"])

    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out.splitlines() == [
        "authenticated_sender_email=sender@example.com",
        "configured_recipient_email=recipient@example.com",
        "can_send=true",
    ]


def test_test_email_uses_sender_check_subject(monkeypatch) -> None:
    import app.cli.test_email as cli

    settings = Settings(
        _env_file=None,
        email_transport="gmail_api",
        gmail_sender_email="sender@example.com",
        gmail_recipient_email="recipient@example.com",
    )
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr(cli, "EmailSender", CapturingSender)

    cli.main([])

    assert CapturingSender.subject == "[Telegram Detox][Test] Gmail sender check"


def test_failed_account_check_still_prints_only_safe_fields(monkeypatch, capsys) -> None:
    import app.cli.test_email as cli

    settings = Settings(
        _env_file=None,
        email_transport="gmail_api",
        gmail_sender_email="sender@example.com",
        gmail_recipient_email="recipient@example.com",
    )
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr(cli, "GmailApiSender", FailingAccountCheckSender)

    with pytest.raises(SystemExit):
        cli.main(["--account-check"])

    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out.splitlines() == [
        "authenticated_sender_email=",
        "configured_recipient_email=recipient@example.com",
        "can_send=false",
    ]
    assert "access_token" not in captured.out


def test_account_check_rejects_insecure_token_with_safe_output(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    import app.cli.test_email as cli

    token_marker = "account-check-token-content-marker"  # noqa: S105
    token_path = tmp_path / "gmail_oauth_token.json"
    token_path.write_text(token_marker, encoding="utf-8")
    token_path.chmod(0o644)
    settings = Settings(
        _env_file=None,
        email_transport="gmail_api",
        gmail_oauth_token_path=token_path,
        gmail_sender_email="sender@example.com",
        gmail_recipient_email="recipient@example.com",
    )

    class NeverReadCredentials:
        @classmethod
        def from_authorized_user_file(cls, path, scopes):
            raise AssertionError("insecure account-check token was read")

    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr("app.email.sender.Credentials", NeverReadCredentials)

    with pytest.raises(SystemExit):
        cli.main(["--account-check"])

    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out.splitlines() == [
        "authenticated_sender_email=",
        "configured_recipient_email=recipient@example.com",
        "can_send=false",
    ]
    assert token_marker not in captured.out
