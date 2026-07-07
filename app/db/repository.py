from __future__ import annotations

import json
from datetime import datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.db.tables import AlertJob, DigestRecord, MessageRecord
from app.models.schemas import DailyDigest, StoredMessage


def _backoff_minutes(attempts: int) -> int:
    return [1, 5, 15, 60][min(attempts, 3)]


def safe_error(exc: Exception | str | None) -> str | None:
    if exc is None:
        return None
    return exc if isinstance(exc, str) else exc.__class__.__name__


def save_message(session: Session, message: StoredMessage) -> None:
    values = message.model_dump(mode="python")
    values["chat_type"] = message.chat_type.value
    values["media_type"] = message.media_type.value
    if session.bind and session.bind.dialect.name == "sqlite":
        stmt = sqlite_insert(MessageRecord).values(**values)
        stmt = stmt.on_conflict_do_nothing(index_elements=["chat_id", "message_id"])
        session.execute(stmt)
    else:
        exists = session.scalar(
            select(MessageRecord.id).where(
                MessageRecord.chat_id == message.chat_id,
                MessageRecord.message_id == message.message_id,
            )
        )
        if not exists:
            session.add(MessageRecord(**values))
    session.commit()


def get_message(session: Session, chat_id: str, message_id: int) -> MessageRecord | None:
    return session.scalar(
        select(MessageRecord).where(
            MessageRecord.chat_id == chat_id,
            MessageRecord.message_id == message_id,
        )
    )


def mark_alert_sent(session: Session, chat_id: str, message_id: int) -> None:
    record = get_message(session, chat_id, message_id)
    if record:
        record.alert_sent = True
        session.commit()


def mark_p0_review_candidate(session: Session, chat_id: str, message_id: int) -> None:
    record = get_message(session, chat_id, message_id)
    if record:
        record.p0_review_candidate = True
        session.commit()


def recent_chat_context(session: Session, chat_id: str, limit: int = 10) -> list[MessageRecord]:
    rows = session.scalars(
        select(MessageRecord)
        .where(MessageRecord.chat_id == chat_id)
        .order_by(MessageRecord.timestamp.desc(), MessageRecord.message_id.desc())
        .limit(limit)
    ).all()
    return list(reversed(rows))


def messages_between(session: Session, start: datetime, end: datetime) -> list[MessageRecord]:
    return list(
        session.scalars(
            select(MessageRecord)
            .where(MessageRecord.timestamp >= start, MessageRecord.timestamp < end)
            .order_by(MessageRecord.chat_title, MessageRecord.timestamp, MessageRecord.message_id)
        )
    )


def save_digest(session: Session, digest: DailyDigest, html: str) -> DigestRecord:
    record = DigestRecord(
        digest_date=digest.date,
        created_at=datetime.now().astimezone(),
        json_payload=digest.model_dump_json(),
        html_payload=html,
        generated_by=digest.generated_by,
        email_status=digest.email_status,
        error_summary=digest.error_summary,
    )
    session.add(record)
    session.commit()
    return record


def mark_digest_sent(session: Session, record: DigestRecord) -> None:
    record.email_status = "sent"
    record.last_error_safe = None
    record.next_attempt_at = None
    session.commit()


def mark_digest_pending(
    session: Session,
    record: DigestRecord,
    error: Exception | str,
    now: datetime,
) -> None:
    record.email_status = "pending"
    record.attempts += 1
    record.last_error_safe = safe_error(error)
    record.next_attempt_at = now + timedelta(minutes=_backoff_minutes(record.attempts - 1))
    session.commit()


def pending_digests(session: Session) -> list[DigestRecord]:
    return list(
        session.scalars(
            select(DigestRecord)
            .where(DigestRecord.email_status == "pending")
            .order_by(DigestRecord.created_at)
        )
    )


def retry_pending_digests(session: Session, email_sender, now: datetime) -> int:
    records = list(
        session.scalars(
            select(DigestRecord)
            .where(DigestRecord.email_status == "pending")
            .where(
                (DigestRecord.next_attempt_at.is_(None))
                | (DigestRecord.next_attempt_at <= now)
                | (DigestRecord.attempts == 1)
            )
            .order_by(DigestRecord.created_at)
        )
    )
    sent = 0
    for record in records:
        record.email_status = "sending"
        session.commit()
        try:
            email_sender.send(
                f"[FALLBACK] Telegram digest — {record.digest_date}"
                if record.generated_by == "fallback"
                else f"Telegram digest — {record.digest_date}",
                DailyDigest.model_validate_json(record.json_payload).model_dump_json(),
                record.html_payload,
            )
        except Exception as exc:
            mark_digest_pending(session, record, exc, now)
        else:
            mark_digest_sent(session, record)
            sent += 1
    return sent


def create_alert_job(
    session: Session,
    *,
    chat_id: str,
    message_id: int,
    alert_type: str,
    subject: str,
    text_body: str,
    html_body: str,
    now: datetime,
) -> AlertJob:
    existing = session.scalar(
        select(AlertJob).where(
            AlertJob.chat_id == chat_id,
            AlertJob.message_id == message_id,
            AlertJob.alert_type == alert_type,
        )
    )
    if existing:
        return existing
    job = AlertJob(
        chat_id=chat_id,
        message_id=message_id,
        alert_type=alert_type,
        subject=subject,
        text_body=text_body,
        html_body=html_body,
        status="pending",
        attempts=0,
        created_at=now,
    )
    session.add(job)
    session.commit()
    return job


def pending_alert_jobs(session: Session) -> list[AlertJob]:
    return list(
        session.scalars(
            select(AlertJob)
            .where(AlertJob.status == "pending")
            .order_by(AlertJob.created_at, AlertJob.id)
        )
    )


def send_alert_job(session: Session, job: AlertJob, email_sender, now: datetime) -> bool:
    if job.status == "sent":
        return False
    job.status = "sending"
    session.commit()
    try:
        email_sender.send(
            job.subject,
            job.text_body,
            job.html_body,
            message_id=f"<telegram-digest-{job.chat_id}-{job.message_id}-{job.alert_type}@local>",
        )
    except TypeError:
        try:
            email_sender.send(job.subject, job.text_body, job.html_body)
        except Exception as exc:
            job.status = "pending"
            job.attempts += 1
            job.last_error_safe = safe_error(exc)
            job.next_attempt_at = now + timedelta(minutes=_backoff_minutes(job.attempts - 1))
            session.commit()
            return False
    except Exception as exc:
        job.status = "pending"
        job.attempts += 1
        job.last_error_safe = safe_error(exc)
        job.next_attempt_at = now + timedelta(minutes=_backoff_minutes(job.attempts - 1))
        session.commit()
        return False
    job.status = "sent"
    job.sent_at = now
    job.last_error_safe = None
    job.next_attempt_at = None
    session.commit()
    return True


def retry_pending_alerts(session: Session, email_sender, now: datetime) -> int:
    jobs = list(
        session.scalars(
            select(AlertJob)
            .where(AlertJob.status == "pending")
            .where(
                (AlertJob.next_attempt_at.is_(None))
                | (AlertJob.next_attempt_at <= now)
                | (AlertJob.attempts == 1)
            )
            .order_by(AlertJob.created_at, AlertJob.id)
        )
    )
    sent = 0
    for job in jobs:
        sent += int(send_alert_job(session, job, email_sender, now))
    return sent


def cleanup_old(
    session: Session,
    raw_days: int,
    digest_days: int,
    now: datetime,
) -> tuple[int, int]:
    raw_cutoff = now - timedelta(days=raw_days)
    digest_cutoff = now - timedelta(days=digest_days)
    raw = session.execute(
        delete(MessageRecord).where(MessageRecord.timestamp < raw_cutoff)
    ).rowcount
    digests = session.execute(
        delete(DigestRecord).where(DigestRecord.created_at < digest_cutoff)
    ).rowcount
    session.commit()
    return int(raw or 0), int(digests or 0)


def digest_from_record(record: DigestRecord) -> DailyDigest:
    return DailyDigest.model_validate(json.loads(record.json_payload))
