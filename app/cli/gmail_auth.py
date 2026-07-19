from __future__ import annotations

import os
import shutil
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from app.config import Settings, get_settings
from app.email.sender import (
    GMAIL_OAUTH_SCOPES,
    _atomic_write_secret,
    _chmod_600,
    _verify_mode_600,
    authenticated_sender_email,
)

try:
    from google_auth_oauthlib.flow import InstalledAppFlow
except ImportError:  # pragma: no cover - exercised only without optional deps installed
    InstalledAppFlow = None

SUCCESS_MESSAGE = (
    "Gmail token saved. Run test_email --account-check, then test_email to verify sending."
)


def _backup_existing_token(path: Path) -> Path | None:
    if not path.is_file():
        return None
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S_%f")
    backup = path.with_name(f"{path.name}.bak.{timestamp}.{uuid4().hex[:8]}")
    descriptor: int | None = None
    try:
        descriptor = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with path.open("rb") as source, os.fdopen(descriptor, "wb") as target:
            descriptor = None
            shutil.copyfileobj(source, target)
            target.flush()
            os.fsync(target.fileno())
        _chmod_600(backup)
        _verify_mode_600(backup)
        return backup
    except Exception:
        if descriptor is not None:
            os.close(descriptor)
        try:
            backup.unlink(missing_ok=True)
        except OSError as cleanup_exc:
            raise RuntimeError(
                "Could not remove an incomplete Gmail token backup."
            ) from cleanup_exc
        raise


def run_gmail_auth(
    settings: Settings,
    *,
    flow_cls=None,
    account_email_getter=authenticated_sender_email,
) -> None:
    if settings.email_transport != "gmail_api":
        raise RuntimeError("EMAIL_TRANSPORT must be gmail_api for Gmail OAuth setup")
    if not settings.gmail_sender_email.strip():
        raise RuntimeError("GMAIL_SENDER_EMAIL is required for Gmail OAuth setup")
    if not settings.gmail_oauth_client_secret_path.exists():
        raise RuntimeError(
            f"Gmail OAuth client secret missing: {settings.gmail_oauth_client_secret_path}"
        )
    flow_cls = flow_cls or InstalledAppFlow
    if flow_cls is None:
        raise RuntimeError("Gmail OAuth dependencies are not installed")

    flow = flow_cls.from_client_secrets_file(
        str(settings.gmail_oauth_client_secret_path),
        GMAIL_OAUTH_SCOPES,
    )
    print("Log in as the sender account.")
    credentials = flow.run_local_server(port=0)
    authenticated = account_email_getter(credentials)
    if authenticated.casefold() != settings.gmail_sender_email.strip().casefold():
        raise RuntimeError("Authenticated Gmail account does not match GMAIL_SENDER_EMAIL")
    _backup_existing_token(settings.gmail_oauth_token_path)
    _atomic_write_secret(settings.gmail_oauth_token_path, credentials.to_json())
    try:
        _verify_mode_600(settings.gmail_oauth_token_path)
    except Exception:
        try:
            settings.gmail_oauth_token_path.unlink(missing_ok=True)
        except OSError as cleanup_exc:
            raise RuntimeError("Could not remove an insecure Gmail OAuth token.") from cleanup_exc
        raise
    _chmod_600(settings.gmail_oauth_client_secret_path)
    _verify_mode_600(settings.gmail_oauth_client_secret_path)


def main() -> None:
    run_gmail_auth(get_settings())
    print(SUCCESS_MESSAGE)


if __name__ == "__main__":
    main()
