from __future__ import annotations

import sys

from app.config import get_settings
from app.email.sender import EmailSender, EmailSendError


def main() -> None:
    settings = get_settings()
    try:
        EmailSender(settings).send(
            "Telegram digest test email",
            "Plain-text fallback works.",
            "<p>HTML email works.</p>",
        )
    except EmailSendError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from exc
    print("Test email sent.")


if __name__ == "__main__":
    main()
