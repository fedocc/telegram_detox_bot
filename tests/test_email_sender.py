from __future__ import annotations

import smtplib

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.email.sender import EmailSender


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
        smtp_host="smtp.example.com",
        smtp_port=port,
        smtp_tls_mode=mode,
        smtp_username="user",
        smtp_password="",
        email_from="from@example.com",
        email_to="to@example.com",
    )


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
