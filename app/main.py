from __future__ import annotations

import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.config import get_settings
from app.db import repository
from app.db.session import init_db
from app.email.sender import EmailSender
from app.llm.client import HaikuClient
from app.logging_config import configure_logging
from app.services.digest import generate_digest, send_and_store_digest
from app.telegram.client import run_listener


async def main() -> None:
    configure_logging()
    settings = get_settings()
    session_factory = init_db(settings)
    scheduler = AsyncIOScheduler(timezone=settings.timezone)
    hour, minute = [int(part) for part in settings.digest_time.split(":", 1)]

    def daily_job() -> None:
        now = datetime.now(ZoneInfo(settings.timezone))
        with session_factory() as session:
            llm = HaikuClient(settings)
            digest = generate_digest(session, llm, now.date(), settings.timezone)
            send_and_store_digest(session, digest, EmailSender(settings))
            repository.cleanup_old(
                session,
                settings.raw_retention_days,
                settings.digest_retention_days,
                now,
            )

    scheduler.add_job(daily_job, "cron", hour=hour, minute=minute)
    scheduler.start()
    await run_listener(settings, session_factory)


if __name__ == "__main__":
    asyncio.run(main())
