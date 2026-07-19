from __future__ import annotations

import asyncio
import logging
import operator
from datetime import datetime
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.birthdays.service import poll_birthdays_once, run_daily_birthday_reminders
from app.config import get_settings
from app.db import repository
from app.db.session import init_db
from app.email.sender import EmailSender
from app.ignored_chats import load_ignored_chats_from_settings
from app.llm.client import HaikuClient
from app.logging_config import configure_logging
from app.services.maintenance import run_cleanup, run_daily_job
from app.telegram.client import run_listener

logger = logging.getLogger(__name__)


async def main() -> None:
    settings = get_settings()
    configure_logging(settings)
    ignored_chat_ids = load_ignored_chats_from_settings(settings).chat_ids
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
                ignored_chat_ids=ignored_chat_ids,
                mention_usernames=settings.p0_mention_usernames,
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
        current_ignored_chat_ids = load_ignored_chats_from_settings(settings).chat_ids
        with session_factory() as session:
            repository.retry_pending_alerts(
                session,
                EmailSender(settings),
                now,
                excluded_chat_ids=current_ignored_chat_ids,
            )

    def retry_digests_job() -> None:
        now = datetime.now(ZoneInfo(settings.timezone))
        current_ignored_chat_ids = load_ignored_chats_from_settings(settings).chat_ids
        with session_factory() as session:
            repository.retry_pending_digests(
                session,
                EmailSender(settings),
                now,
                ignored_chat_ids=current_ignored_chat_ids,
            )

    def birthday_daily_job() -> None:
        now = datetime.now(ZoneInfo(settings.timezone))
        try:
            with session_factory() as session:
                run_daily_birthday_reminders(
                    session,
                    EmailSender(settings),
                    settings,
                    now,
                )
        except Exception:
            logger.exception("Birthday daily check failed")

    async def birthday_poll_job(client) -> None:
        now = datetime.now(ZoneInfo(settings.timezone))
        try:
            with session_factory() as session:
                await poll_birthdays_once(
                    session,
                    client,
                    EmailSender(settings),
                    settings,
                    now,
                )
        except Exception:
            logger.exception("Birthday poll failed")

    def register_birthday_poll(client) -> None:
        scheduler.add_job(
            birthday_poll_job,
            "interval",
            hours=settings.birthday_poll_interval_hours,
            args=[client],
            next_run_time=datetime.now(ZoneInfo(settings.timezone)),
            id="birthday_poll",
            replace_existing=True,
        )

    scheduler.add_job(daily_job, "cron", hour=hour, minute=minute)
    scheduler.add_job(cleanup_job, "cron", hour=3, minute=10)
    scheduler.add_job(retry_alerts_job, "interval", minutes=1)
    scheduler.add_job(retry_digests_job, "interval", minutes=5)
    if settings.birthday_reminders_enabled:
        birthday_hour, birthday_minute = [
            int(part) for part in settings.birthday_reminder_time.split(":", 1)
        ]
        scheduler.add_job(
            birthday_daily_job,
            "cron",
            hour=birthday_hour,
            minute=birthday_minute,
            id="birthday_daily",
            replace_existing=True,
        )
    operator.methodcaller("start")(scheduler)
    await run_listener(
        settings,
        session_factory,
        on_connected=register_birthday_poll if settings.birthday_reminders_enabled else None,
        ignored_chat_ids=ignored_chat_ids,
    )


if __name__ == "__main__":
    asyncio.run(main())
