from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from app.config import get_settings
from app.db.session import init_db
from app.email.sender import EmailSender
from app.llm.client import HaikuClient
from app.services.digest import send_daily_digest_pipeline


def main() -> None:
    settings = get_settings()
    session_factory = init_db(settings)
    now = datetime.now(ZoneInfo(settings.timezone))
    with session_factory() as session:
        send_daily_digest_pipeline(
            session,
            HaikuClient(settings),
            EmailSender(settings),
            now.date(),
            settings.timezone,
        )
    print("Digest sent.")


if __name__ == "__main__":
    main()
