from __future__ import annotations

from app.config import get_settings
from app.email.sender import EmailSender


def main() -> None:
    settings = get_settings()
    EmailSender(settings).send(
        "Telegram digest test email",
        "Plain-text fallback works.",
        "<p>HTML email works.</p>",
    )
    print("Test email sent.")


if __name__ == "__main__":
    main()

