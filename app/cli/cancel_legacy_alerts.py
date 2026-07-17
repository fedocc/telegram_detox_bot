from __future__ import annotations

from app.config import get_settings
from app.db import repository
from app.db.session import init_db


def main() -> None:
    settings = get_settings()
    session_factory = init_db(settings)
    with session_factory() as session:
        cancelled = repository.cancel_legacy_alerts(session)
    print(f"Cancelled legacy alert jobs: {cancelled}")


if __name__ == "__main__":
    main()
