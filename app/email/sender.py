from __future__ import annotations

import base64
import json
import os
import smtplib
import ssl
import stat
import tempfile
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
GOOGLE_IDENTITY_SCOPES = ["https://www.googleapis.com/auth/userinfo.email"]
GMAIL_OAUTH_SCOPES = [*GMAIL_SEND_SCOPES, *GOOGLE_IDENTITY_SCOPES]


class EmailSendError(RuntimeError):
    pass


class CredentialPermissionError(RuntimeError):
    pass


class CredentialFileMissingError(CredentialPermissionError):
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
    except OSError as exc:
        raise CredentialPermissionError(
            "Could not set credential file permissions to 600."
        ) from exc


def _verify_mode_600(path: Path) -> None:
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except FileNotFoundError:
        raise CredentialFileMissingError("Credential file is missing.") from None
    except OSError as exc:
        raise CredentialPermissionError("Could not verify credential file permissions.") from exc
    if mode != 0o600:
        raise CredentialPermissionError("Credential file permissions must be 600.")


def _atomic_write_secret(path: Path, text: str) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp_path: Path | None = None
    replaced = False
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as tmp:
            tmp.write(text)
            tmp.flush()
            os.fsync(tmp.fileno())
            tmp_path = Path(tmp.name)
        _chmod_600(tmp_path)
        _verify_mode_600(tmp_path)
        os.replace(tmp_path, path)
        tmp_path = None
        replaced = True
        _chmod_600(path)
        _verify_mode_600(path)
    except Exception:
        if tmp_path is not None:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError as cleanup_exc:
                raise CredentialPermissionError(
                    "Could not remove an incomplete credential file."
                ) from cleanup_exc
        if replaced:
            try:
                _verify_mode_600(path)
            except CredentialPermissionError:
                try:
                    path.unlink(missing_ok=True)
                except OSError as cleanup_exc:
                    raise CredentialPermissionError(
                        "Could not remove an insecure credential file."
                    ) from cleanup_exc
        raise


def _build_message(
    sender_email: str,
    recipient_email: str,
    subject: str,
    text: str,
    html: str | None = None,
    *,
    message_id: str | None = None,
) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender_email
    msg["To"] = recipient_email
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


def authenticated_sender_email(credentials) -> str:
    if build is None:
        raise EmailSendError("Google account check dependencies missing")
    try:
        service = build("oauth2", "v2", credentials=credentials, cache_discovery=False)
        profile = service.userinfo().get().execute()
    except Exception as exc:
        raise EmailSendError("Google account check failed") from exc
    email = profile.get("email") if isinstance(profile, dict) else None
    if not isinstance(email, str) or not email.strip():
        raise EmailSendError("Google account check returned no email")
    return email.strip()


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
        msg = _build_message(
            self.settings.email_from,
            self.settings.email_to,
            subject,
            text,
            html,
            message_id=message_id,
        )
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
        try:
            _verify_mode_600(token_path)
        except CredentialFileMissingError:
            raise EmailSendError(
                "Gmail OAuth token missing. Run: .venv/bin/python -m app.cli.gmail_auth"
            ) from None
        except CredentialPermissionError:
            raise EmailSendError(
                "Gmail token file permissions are insecure; expected 0600"
            ) from None
        try:
            creds = Credentials.from_authorized_user_file(
                str(token_path),
                scopes=GMAIL_OAUTH_SCOPES,
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
            try:
                _atomic_write_secret(token_path, creds.to_json())
            except CredentialPermissionError as exc:
                raise EmailSendError(str(exc)) from exc
        if not getattr(creds, "valid", True):
            raise EmailSendError("Gmail OAuth token invalid")
        return creds

    def _account_status_for_credentials(self, credentials) -> tuple[str, str, bool]:
        recipient = self.settings.gmail_recipient_email.strip()
        authenticated = authenticated_sender_email(credentials)
        configured_sender = self.settings.gmail_sender_email.strip()
        can_send = bool(
            recipient
            and configured_sender
            and authenticated.casefold() == configured_sender.casefold()
        )
        return authenticated, recipient, can_send

    def account_status(self) -> tuple[str, str, bool]:
        return self._account_status_for_credentials(self._credentials())

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
        authenticated, recipient, can_send = self._account_status_for_credentials(creds)
        if not self.settings.gmail_sender_email.strip():
            raise EmailSendError("GMAIL_SENDER_EMAIL is required")
        if not recipient:
            raise EmailSendError("GMAIL_RECIPIENT_EMAIL is required")
        if not can_send:
            raise EmailSendError("Authenticated Gmail account does not match GMAIL_SENDER_EMAIL")
        msg = _build_message(
            authenticated,
            recipient,
            subject,
            text,
            html,
            message_id=message_id,
        )
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
