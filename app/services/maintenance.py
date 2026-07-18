from __future__ import annotations

import logging
from datetime import date, datetime

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
    *,
    ignored_chat_ids: frozenset[str] | set[str] | None = None,
) -> None:
    try:
        send_daily_digest_pipeline(
            session,
            llm,
            email_sender,
            day,
            timezone,
            ignored_chat_ids=ignored_chat_ids,
        )
    except Exception:
        logger.exception("Daily digest pipeline failed")
    finally:
        run_cleanup(session, raw_retention_days, digest_retention_days, now)
