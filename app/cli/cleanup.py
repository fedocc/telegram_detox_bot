from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from app.config import get_settings
from app.db import repository
from app.db.session import init_db


def run(settings, *, output: Callable[[str], None] = print) -> tuple[int, int, int]:
    session_factory = init_db(settings)
    with session_factory() as session:
        cancelled = repository.cancel_legacy_alerts(session)
        raw, digests = repository.cleanup_old(
            session,
            settings.raw_retention_days,
            settings.digest_retention_days,
            datetime.now().astimezone(),
        )
    output(
        f"Cancelled unsafe alert jobs: {cancelled}; deleted raw messages: {raw}; digests: {digests}"
    )
    return cancelled, raw, digests


def main() -> None:
    run(get_settings())


if __name__ == "__main__":
    main()
