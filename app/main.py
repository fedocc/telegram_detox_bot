from __future__ import annotations

import asyncio
import operator
from datetime import datetime
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.config import get_settings
from app.db import repository
from app.db.session import init_db
from app.email.sender import EmailSender
from app.llm.client import HaikuClient
from app.logging_config import configure_logging
from app.services.maintenance import run_cleanup, run_daily_job
from app.telegram.client import run_listener


async def main() -> None:
    settings = get_settings()
    configure_logging(settings)
    session_factory = init_db(settings)
    scheduler = AsyncIOScheduler(timezone=settings.timezone)
    hour, minute = [int(part) for part in settings.digest_time.split(":", 1)]

    def daily_job() -> None:
        now = datetime.now(ZoneInfo(settings.timezone))
        with session_factory() as session:
            run_daily_job(
                session,
                HaikuClient(settings),
                EmailSender(settings),
                now.date(),
                settings.timezone,
                settings.raw_retention_days,
                settings.digest_retention_days,
                now,
            )

    def cleanup_job() -> None:
        now = datetime.now(ZoneInfo(settings.timezone))
        with session_factory() as session:
            run_cleanup(
                session,
                settings.raw_retention_days,
                settings.digest_retention_days,
                now,
            )

    def retry_alerts_job() -> None:
        now = datetime.now(ZoneInfo(settings.timezone))
        with session_factory() as session:
            repository.retry_pending_alerts(session, EmailSender(settings), now)

    def retry_digests_job() -> None:
        now = datetime.now(ZoneInfo(settings.timezone))
        with session_factory() as session:
            repository.retry_pending_digests(session, EmailSender(settings), now)

    scheduler.add_job(daily_job, "cron", hour=hour, minute=minute)
    scheduler.add_job(cleanup_job, "cron", hour=3, minute=10)
    scheduler.add_job(retry_alerts_job, "interval", minutes=1)
    scheduler.add_job(retry_digests_job, "interval", minutes=5)
    operator.methodcaller("start")(scheduler)
    await run_listener(settings, session_factory)


if __name__ == "__main__":
    asyncio.run(main())
