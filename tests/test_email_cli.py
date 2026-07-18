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


def test_test_email_prints_safe_google_error(monkeypatch, capsys) -> None:
    import app.cli.test_email as cli

    settings = Settings(
        _env_file=None,
        aitunnel_api_key="test",
        email_transport="gmail_api",
        email_from="from@example.com",
        email_to="to@example.com",
    )
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr(cli, "EmailSender", FailingSender)

    with pytest.raises(SystemExit):
        cli.main()

    captured = capsys.readouterr()
    assert "status=403" in captured.err
    assert "reason=insufficientPermissions" in captured.err
    assert "Request had insufficient authentication scopes." in captured.err
    assert "access_token" not in captured.err
