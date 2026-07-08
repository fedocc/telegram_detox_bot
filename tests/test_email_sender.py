from __future__ import annotations

import base64
import json
import logging
import smtplib
from email import message_from_bytes
from email.policy import default
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.email.sender import EmailSender, EmailSendError, GmailApiSender


class FakeSMTP:
    calls: list[tuple] = []

    def __init__(self, host: str, port: int, timeout: int | None = None) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.calls = []
        FakeSMTP.calls.append(("init", host, port, timeout))

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def ehlo(self) -> None:
        self.calls.append(("ehlo",))
        FakeSMTP.calls.append(("ehlo",))

    def starttls(self, context) -> None:
        self.calls.append(("starttls", context))
        FakeSMTP.calls.append(("starttls", context))

    def login(self, username: str, password: str) -> None:
        self.calls.append(("login", username, password))
        FakeSMTP.calls.append(("login", username, password))

    def sendmail(self, from_addr: str, to_addrs: list[str], body: str) -> None:
        self.calls.append(("sendmail", from_addr, to_addrs, body))
        FakeSMTP.calls.append(("sendmail", from_addr, to_addrs, body))


def smtp_settings(mode: str, port: int) -> Settings:
    return Settings(
        aitunnel_api_key="test",
        email_transport="smtp",
        smtp_host="smtp.example.com",
        smtp_port=port,
        smtp_tls_mode=mode,
        smtp_username="user",
        smtp_password="",
        email_from="from@example.com",
        email_to="to@example.com",
    )


def gmail_settings(tmp_path: Path) -> Settings:
    return Settings(
        aitunnel_api_key="test",
        email_transport="gmail_api",
        gmail_oauth_client_secret_path=tmp_path / "google_oauth_client.json",
        gmail_oauth_token_path=tmp_path / "gmail_oauth_token.json",
        email_from="from@example.com",
        email_to="to@example.com",
    )


class FakeExecute:
    def __init__(self, response=None, error: Exception | None = None) -> None:
        self.response = response or {"id": "sent-id"}
        self.error = error

    def execute(self):
        if self.error:
            raise self.error
        return self.response


class FakeMessages:
    def __init__(self, service) -> None:
        self.service = service

    def send(self, userId: str, body: dict):
        self.service.calls.append(("send", userId, body))
        return FakeExecute(error=self.service.error)


class FakeUsers:
    def __init__(self, service) -> None:
        self.service = service

    def messages(self):
        return FakeMessages(self.service)


class FakeGmailService:
    def __init__(self, error: Exception | None = None) -> None:
        self.calls = []
        self.error = error

    def users(self):
        return FakeUsers(self)


class FakeCredentials:
    loaded_path: str | None = None
    refresh_called = False

    def __init__(self, *, expired: bool = False, valid: bool = True, refresh_token: str = "rt"):  # noqa: S107
        self.expired = expired
        self.valid = valid
        self.refresh_token = refresh_token
        self.token = "access"  # noqa: S105

    @classmethod
    def from_authorized_user_file(cls, path, scopes):
        cls.loaded_path = str(path)
        return cls()

    def refresh(self, request) -> None:
        self.refresh_called = True
        FakeCredentials.refresh_called = True
        self.expired = False
        self.valid = True
        self.token = "new-access"  # noqa: S105

    def to_json(self) -> str:
        return json.dumps({"token": self.token, "refresh_token": self.refresh_token})


class ExpiredCredentials(FakeCredentials):
    @classmethod
    def from_authorized_user_file(cls, path, scopes):
        return cls(expired=True, valid=False, refresh_token="rt")  # noqa: S106


class UnrefreshableCredentials(FakeCredentials):
    @classmethod
    def from_authorized_user_file(cls, path, scopes):
        return cls(expired=True, valid=False, refresh_token=None)


def token_file(path: Path) -> None:
    path.write_text('{"token":"old","refresh_token":"rt"}', encoding="utf-8")
    path.chmod(0o600)


def decode_sent_message(service: FakeGmailService):
    raw = service.calls[0][2]["raw"]
    return message_from_bytes(base64.urlsafe_b64decode(raw.encode("ascii")), policy=default)


def test_ssl_mode_uses_smtp_ssl(monkeypatch) -> None:
    FakeSMTP.calls = []
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTP)

    EmailSender(smtp_settings("ssl", 465)).send("subject", "text")

    assert FakeSMTP.calls[0] == ("init", "smtp.example.com", 465, 20)
    assert ("login", "user", "") in FakeSMTP.calls


def test_starttls_mode_uses_smtp_and_starttls(monkeypatch) -> None:
    FakeSMTP.calls = []
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)

    EmailSender(smtp_settings("starttls", 587)).send("subject", "text")

    assert FakeSMTP.calls[0] == ("init", "smtp.example.com", 587, 20)
    assert any(call[0] == "starttls" for call in FakeSMTP.calls)
    assert ("login", "user", "") in FakeSMTP.calls


def test_starttls_calls_ehlo_before_and_after_tls(monkeypatch) -> None:
    FakeSMTP.calls = []
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)

    EmailSender(smtp_settings("starttls", 587)).send("subject", "text")

    call_names = [call[0] for call in FakeSMTP.calls]
    assert call_names.index("ehlo") < call_names.index("starttls")
    assert call_names[call_names.index("starttls") + 1] == "ehlo"


def test_invalid_smtp_tls_mode_fails_clearly() -> None:
    with pytest.raises(ValidationError, match="SMTP_TLS_MODE must be one of"):
        smtp_settings("plain", 25)


def test_smtp_timeout_is_passed_in_both_modes(monkeypatch) -> None:
    FakeSMTP.calls = []
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTP)
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)

    EmailSender(smtp_settings("ssl", 465)).send("subject", "text")
    EmailSender(smtp_settings("starttls", 587)).send("subject", "text")

    assert ("init", "smtp.example.com", 465, 20) in FakeSMTP.calls
    assert ("init", "smtp.example.com", 587, 20) in FakeSMTP.calls


def test_smtp_transport_still_works_when_explicitly_selected(monkeypatch) -> None:
    FakeSMTP.calls = []
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTP)

    EmailSender(smtp_settings("ssl", 465)).send("subject", "text")

    assert FakeSMTP.calls[0][0] == "init"


def test_gmail_api_sender_builds_valid_multipart_message(tmp_path, monkeypatch) -> None:
    settings = gmail_settings(tmp_path)
    token_file(settings.gmail_oauth_token_path)
    service = FakeGmailService()
    monkeypatch.setattr("app.email.sender.Credentials", FakeCredentials)
    monkeypatch.setattr("app.email.sender.build", lambda *args, **kwargs: service)

    GmailApiSender(settings).send("Subject", "plain", "<p>html</p>")

    message = decode_sent_message(service)
    assert message.is_multipart()
    assert message.get_content_type() == "multipart/alternative"
    payload_types = [part.get_content_type() for part in message.iter_parts()]
    assert payload_types == ["text/plain", "text/html"]


def test_gmail_api_sender_sets_from_to_subject_and_message_id(tmp_path, monkeypatch) -> None:
    settings = gmail_settings(tmp_path)
    token_file(settings.gmail_oauth_token_path)
    service = FakeGmailService()
    monkeypatch.setattr("app.email.sender.Credentials", FakeCredentials)
    monkeypatch.setattr("app.email.sender.build", lambda *args, **kwargs: service)

    GmailApiSender(settings).send("Subject", "plain", "<p>html</p>", message_id="<stable@test>")

    message = decode_sent_message(service)
    assert message["From"] == "from@example.com"
    assert message["To"] == "to@example.com"
    assert message["Subject"] == "Subject"
    assert message["Message-ID"] == "<stable@test>"


def test_gmail_api_sender_uses_base64url_raw_payload(tmp_path, monkeypatch) -> None:
    settings = gmail_settings(tmp_path)
    token_file(settings.gmail_oauth_token_path)
    service = FakeGmailService()
    monkeypatch.setattr("app.email.sender.Credentials", FakeCredentials)
    monkeypatch.setattr("app.email.sender.build", lambda *args, **kwargs: service)

    GmailApiSender(settings).send("Subject", "plain", "<p>html</p>")

    raw = service.calls[0][2]["raw"]
    assert "+" not in raw
    assert "/" not in raw
    assert b"Subject: Subject" in base64.urlsafe_b64decode(raw.encode("ascii"))


def test_gmail_api_sender_calls_users_messages_send(tmp_path, monkeypatch) -> None:
    settings = gmail_settings(tmp_path)
    token_file(settings.gmail_oauth_token_path)
    service = FakeGmailService()
    monkeypatch.setattr("app.email.sender.Credentials", FakeCredentials)
    monkeypatch.setattr("app.email.sender.build", lambda *args, **kwargs: service)

    GmailApiSender(settings).send("Subject", "plain", "<p>html</p>")

    assert service.calls[0][0] == "send"
    assert service.calls[0][1] == "me"
    assert set(service.calls[0][2]) == {"raw"}


def test_gmail_api_sender_wraps_google_api_error_as_email_send_error(tmp_path, monkeypatch) -> None:
    settings = gmail_settings(tmp_path)
    token_file(settings.gmail_oauth_token_path)
    service = FakeGmailService(error=RuntimeError("provider token secret"))
    monkeypatch.setattr("app.email.sender.Credentials", FakeCredentials)
    monkeypatch.setattr("app.email.sender.build", lambda *args, **kwargs: service)

    with pytest.raises(EmailSendError, match="Gmail API send failed"):
        GmailApiSender(settings).send("Subject", "plain", "<p>html</p>")


def test_expired_token_is_refreshed_and_saved_atomically(tmp_path, monkeypatch) -> None:
    settings = gmail_settings(tmp_path)
    token_file(settings.gmail_oauth_token_path)
    service = FakeGmailService()
    monkeypatch.setattr("app.email.sender.Credentials", ExpiredCredentials)
    monkeypatch.setattr("app.email.sender.build", lambda *args, **kwargs: service)

    GmailApiSender(settings).send("Subject", "plain", "<p>html</p>")

    saved = json.loads(settings.gmail_oauth_token_path.read_text(encoding="utf-8"))
    assert saved["token"] == "new-access"  # noqa: S105
    assert not list(tmp_path.glob("*.tmp"))


def test_refreshed_token_file_has_600_permissions(tmp_path, monkeypatch) -> None:
    settings = gmail_settings(tmp_path)
    token_file(settings.gmail_oauth_token_path)
    service = FakeGmailService()
    monkeypatch.setattr("app.email.sender.Credentials", ExpiredCredentials)
    monkeypatch.setattr("app.email.sender.build", lambda *args, **kwargs: service)

    GmailApiSender(settings).send("Subject", "plain", "<p>html</p>")

    assert settings.gmail_oauth_token_path.stat().st_mode & 0o777 == 0o600


def test_missing_gmail_token_fails_clearly(tmp_path) -> None:
    settings = gmail_settings(tmp_path)

    with pytest.raises(EmailSendError, match="Gmail OAuth token missing"):
        GmailApiSender(settings).send("Subject", "plain", "<p>html</p>")


def test_gmail_token_and_client_secret_never_appear_in_logs(tmp_path, monkeypatch, caplog) -> None:
    settings = gmail_settings(tmp_path)
    token_file(settings.gmail_oauth_token_path)
    settings.gmail_oauth_client_secret_path.write_text("client-secret-value", encoding="utf-8")
    service = FakeGmailService(error=RuntimeError("refresh-token-value"))
    monkeypatch.setattr("app.email.sender.Credentials", FakeCredentials)
    monkeypatch.setattr("app.email.sender.build", lambda *args, **kwargs: service)

    with caplog.at_level(logging.ERROR):
        with pytest.raises(EmailSendError):
            GmailApiSender(settings).send("Subject", "plain", "<p>html</p>")

    assert "refresh-token-value" not in caplog.text
    assert "client-secret-value" not in caplog.text
