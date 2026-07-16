from __future__ import annotations

import logging
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.db import repository
from app.email.sender import EmailSender
from app.llm.client import HaikuClient
from app.services.digest import send_daily_digest_pipeline

logger = logging.getLogger(__name__)


def run_cleanup(
    session: Session,
    raw_retention_days: int,
    digest_retention_days: int,
    now: datetime,
) -> tuple[int, int]:
    try:
        raw, digests = repository.cleanup_old(
            session,
            raw_retention_days,
            digest_retention_days,
            now,
        )
        logger.info("Cleanup deleted raw_messages=%s digests=%s", raw, digests)
        return raw, digests
    except Exception:
        logger.exception("Cleanup failed")
        return 0, 0


def run_daily_job(
    session: Session,
    llm: HaikuClient,
    email_sender: EmailSender,
    day: date,
    timezone: str,
    raw_retention_days: int,
    digest_retention_days: int,
    now: datetime,
) -> None:
    try:
        send_daily_digest_pipeline(session, llm, email_sender, day, timezone)
    except Exception:
        logger.exception("Daily digest pipeline failed")
    finally:
        run_cleanup(session, raw_retention_days, digest_retention_days, now)


def missed_digest_dates(session: Session, timezone: str, now: datetime) -> list[date]:
    tz = ZoneInfo(timezone)
    now_local = now.astimezone(tz) if now.tzinfo else now.replace(tzinfo=UTC).astimezone(tz)
    today = now_local.date()
    today_start_local = datetime.combine(today, datetime.min.time(), tzinfo=tz)
    timestamps = repository.undigested_message_timestamps_before(
        session,
        today_start_local.astimezone(UTC),
    )
    dates = {
        (ts.replace(tzinfo=UTC) if ts.tzinfo is None else ts.astimezone(UTC))
        .astimezone(tz)
        .date()
        for ts in timestamps
    }
    return sorted(day for day in dates if day < today)


def recover_missed_daily_digests(
    session: Session,
    llm: HaikuClient,
    email_sender: EmailSender,
    timezone: str,
    now: datetime,
) -> list[date]:
    recovered: list[date] = []
    for day in missed_digest_dates(session, timezone, now):
        try:
            send_daily_digest_pipeline(session, llm, email_sender, day, timezone)
            recovered.append(day)
        except Exception:
            logger.exception("Missed daily digest recovery failed for date=%s", day.isoformat())
    if recovered:
        logger.info("Recovered missed daily digests count=%s", len(recovered))
    return recovered
