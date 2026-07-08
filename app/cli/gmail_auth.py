from __future__ import annotations

from app.config import Settings, get_settings
from app.email.sender import GMAIL_SEND_SCOPES, _atomic_write_secret, _chmod_600

try:
    from google_auth_oauthlib.flow import InstalledAppFlow
except ImportError:  # pragma: no cover - exercised only without optional deps installed
    InstalledAppFlow = None

SUCCESS_MESSAGE = "Gmail token saved. Run test_email to verify sending account."


def run_gmail_auth(
    settings: Settings,
    *,
    flow_cls=None,
) -> None:
    if settings.email_transport != "gmail_api":
        raise RuntimeError("EMAIL_TRANSPORT must be gmail_api for Gmail OAuth setup")
    if not settings.email_from:
        raise RuntimeError("EMAIL_FROM is required for Gmail OAuth setup")
    if not settings.gmail_oauth_client_secret_path.exists():
        raise RuntimeError(
            f"Gmail OAuth client secret missing: {settings.gmail_oauth_client_secret_path}"
        )
    flow_cls = flow_cls or InstalledAppFlow
    if flow_cls is None:
        raise RuntimeError("Gmail OAuth dependencies are not installed")

    flow = flow_cls.from_client_secrets_file(
        str(settings.gmail_oauth_client_secret_path),
        GMAIL_SEND_SCOPES,
    )
    credentials = flow.run_local_server(port=0)
    _atomic_write_secret(settings.gmail_oauth_token_path, credentials.to_json())
    _chmod_600(settings.gmail_oauth_client_secret_path)


def main() -> None:
    run_gmail_auth(get_settings())
    print(SUCCESS_MESSAGE)


if __name__ == "__main__":
    main()
