from __future__ import annotations

import argparse
import sys

from app.config import get_settings
from app.email.sender import EmailSender, EmailSendError, GmailApiSender

TEST_SUBJECT = "[Telegram Detox][Test] Gmail sender check"


def _account_check() -> None:
    authenticated = ""
    recipient = ""
    can_send = False
    try:
        settings = get_settings()
        recipient = settings.gmail_recipient_email.strip()
        if settings.email_transport == "gmail_api":
            authenticated, recipient, can_send = GmailApiSender(settings).account_status()
    except Exception:
        authenticated = ""
        can_send = False
    print(f"authenticated_sender_email={authenticated}")
    print(f"configured_recipient_email={recipient}")
    print(f"can_send={str(can_send).lower()}")
    if not can_send:
        raise SystemExit(1)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Check Gmail sender or send a test email.")
    parser.add_argument("--account-check", action="store_true")
    args = parser.parse_args(argv)
    if args.account_check:
        _account_check()
        return

    settings = get_settings()
    try:
        EmailSender(settings).send(
            TEST_SUBJECT,
            "Plain-text fallback works.",
            "<p>HTML email works.</p>",
        )
    except EmailSendError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from exc
    print("Test email sent.")


if __name__ == "__main__":
    main()
