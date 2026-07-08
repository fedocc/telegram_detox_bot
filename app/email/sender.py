from __future__ import annotations

import base64
import json
import os
import smtplib
import ssl
from email.message import EmailMessage
from pathlib import Path

from app.config import Settings

try:
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError
except ImportError:  # pragma: no cover - exercised only without optional deps installed
    Request = None
    Credentials = None
    build = None
    HttpError = None

GMAIL_SEND_SCOPES = ["https://www.googleapis.com/auth/gmail.send"]


class EmailSendError(RuntimeError):
    pass


GMAIL_ERROR_HINTS = {
    (403, "insufficientPermissions"): "Delete Gmail OAuth token and rerun gmail_auth.",
    (403, "accessNotConfigured"): "enable Gmail API in Google Cloud project.",
    (400, "invalidArgument"): "Check MIME/raw payload issue.",
    (401, "invalidCredentials"): "Delete Gmail OAuth token and rerun gmail_auth.",
    (403, "forbidden"): "Check Gmail account or workspace restrictions.",
    (403, "failedPrecondition"): "Check Gmail account or workspace restrictions.",
}


def _chmod_600(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _atomic_write_secret(path: Path, text: str) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text, encoding="utf-8")
    _chmod_600(tmp)
    os.replace(tmp, path)
    _chmod_600(path)


def _build_message(
    settings: Settings,
    subject: str,
    text: str,
    html: str | None = None,
    *,
    message_id: str | None = None,
) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = settings.email_from
    msg["To"] = settings.email_to
    if message_id:
        msg["Message-ID"] = message_id
    msg.set_content(text)
    if html:
        msg.add_alternative(html, subtype="html")
    return msg


def _safe_text(value: object, *, limit: int = 300) -> str:
    text = str(value or "").replace("\n", " ").replace("\r", " ").strip()
    for marker in ["access_token", "refresh_token", "client_secret", "Authorization", "Bearer"]:
        if marker.lower() in text.lower():
            return "[REDACTED]"
    return text[:limit]


def _gmail_http_error_summary(exc) -> str:
    status = int(getattr(getattr(exc, "resp", None), "status", 0) or 0)
    http_reason = _safe_text(getattr(getattr(exc, "resp", None), "reason", ""))
    google_reason = http_reason
    google_message = http_reason or "Gmail API request failed."
    try:
        payload = json.loads(exc.content.decode("utf-8"))
        error = payload.get("error", {})
        google_message = _safe_text(error.get("message") or google_message)
        errors = error.get("errors") or []
        if errors and isinstance(errors[0], dict):
            google_reason = _safe_text(errors[0].get("reason") or google_reason)
    except (AttributeError, TypeError, UnicodeDecodeError, ValueError, json.JSONDecodeError):
        google_message = google_message or "Gmail API request failed."

    lines = [
        "Gmail API send failed:",
        f"status={status}",
        f"reason={google_reason or 'unknown'}",
        f"message={google_message or 'Gmail API request failed.'}",
    ]
    hint = GMAIL_ERROR_HINTS.get((status, google_reason))
    if hint:
        lines.append(f"hint={hint}")
    return "\n".join(lines)


class SmtpEmailSender:
    def __init__(self, settings: Settings):
        self.settings = settings

    def send(
        self,
        subject: str,
        text: str,
        html: str | None = None,
        *,
        message_id: str | None = None,
    ) -> None:
        msg = _build_message(self.settings, subject, text, html, message_id=message_id)
        try:
            if self.settings.smtp_tls_mode == "ssl":
                with smtplib.SMTP_SSL(
                    self.settings.smtp_host,
                    self.settings.smtp_port,
                    timeout=20,
                ) as smtp:
                    smtp.login(self.settings.smtp_username, self.settings.smtp_password)
                    smtp.sendmail(
                        self.settings.email_from,
                        [self.settings.email_to],
                        msg.as_string(),
                    )
            elif self.settings.smtp_tls_mode == "starttls":
                with smtplib.SMTP(
                    self.settings.smtp_host,
                    self.settings.smtp_port,
                    timeout=20,
                ) as smtp:
                    smtp.ehlo()
                    smtp.starttls(context=ssl.create_default_context())
                    smtp.ehlo()
                    smtp.login(self.settings.smtp_username, self.settings.smtp_password)
                    smtp.sendmail(
                        self.settings.email_from,
                        [self.settings.email_to],
                        msg.as_string(),
                    )
            else:
                raise EmailSendError("Invalid SMTP_TLS_MODE")
        except (smtplib.SMTPException, OSError, TimeoutError) as exc:
            raise EmailSendError("SMTP send failed") from exc


class GmailApiSender:
    def __init__(self, settings: Settings):
        self.settings = settings

    def _credentials(self):
        if Credentials is None or Request is None:
            raise EmailSendError("Gmail API dependencies missing")
        token_path = self.settings.gmail_oauth_token_path
        if not token_path.exists():
            raise EmailSendError(
                "Gmail OAuth token missing. Run: .venv/bin/python -m app.cli.gmail_auth"
            )
        try:
            creds = Credentials.from_authorized_user_file(
                str(token_path),
                scopes=GMAIL_SEND_SCOPES,
            )
        except Exception as exc:
            raise EmailSendError("Gmail OAuth token invalid") from exc
        if getattr(creds, "expired", False):
            if not getattr(creds, "refresh_token", None):
                raise EmailSendError("Gmail OAuth token cannot be refreshed")
            try:
                creds.refresh(Request())
            except Exception as exc:
                raise EmailSendError("Gmail OAuth token refresh failed") from exc
            _atomic_write_secret(token_path, creds.to_json())
        if not getattr(creds, "valid", True):
            raise EmailSendError("Gmail OAuth token invalid")
        return creds

    def send(
        self,
        subject: str,
        text: str,
        html: str | None = None,
        *,
        message_id: str | None = None,
    ) -> None:
        if build is None:
            raise EmailSendError("Gmail API dependencies missing")
        creds = self._credentials()
        msg = _build_message(self.settings, subject, text, html, message_id=message_id)
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")
        try:
            service = build("gmail", "v1", credentials=creds, cache_discovery=False)
            service.users().messages().send(userId="me", body={"raw": raw}).execute()
        except HttpError as exc:
            raise EmailSendError(_gmail_http_error_summary(exc)) from exc
        except Exception as exc:
            raise EmailSendError("Gmail API send failed") from exc


class EmailSender:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.transport = (
            GmailApiSender(settings)
            if settings.email_transport == "gmail_api"
            else SmtpEmailSender(settings)
        )

    def send(
        self,
        subject: str,
        text: str,
        html: str | None = None,
        *,
        message_id: str | None = None,
    ) -> None:
        self.transport.send(subject, text, html, message_id=message_id)
