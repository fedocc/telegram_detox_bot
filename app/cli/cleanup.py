from __future__ import annotations

from datetime import datetime

from app.config import get_settings
from app.db import repository
from app.db.session import init_db


def main() -> None:
    settings = get_settings()
    session_factory = init_db(settings)
    with session_factory() as session:
        raw, digests = repository.cleanup_old(
            session,
            settings.raw_retention_days,
            settings.digest_retention_days,
            datetime.now().astimezone(),
        )
    print(f"Deleted raw messages: {raw}; digests: {digests}")


if __name__ == "__main__":
    main()
