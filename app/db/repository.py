from __future__ import annotations

import json
from datetime import datetime, timedelta
from uuid import uuid4

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.db.tables import AlertJob, DigestRecord, MessageRecord
from app.models.schemas import DailyDigest, StoredMessage


def _backoff_minutes(attempts: int) -> int:
    return [1, 5, 15, 60][min(max(attempts - 1, 0), 3)]


def safe_error(exc: Exception | str | None) -> str | None:
    if exc is None:
        return None
    return exc if isinstance(exc, str) else exc.__class__.__name__


def _db_time(value: datetime) -> datetime:
    return value.replace(tzinfo=None) if value.tzinfo else value


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


def save_digest(
    session: Session,
    digest: DailyDigest,
    html: str,
    *,
    subject: str = "",
    text: str = "",
) -> DigestRecord:
    now = _db_time(datetime.now().astimezone())
    record = DigestRecord(
        digest_date=digest.date,
        created_at=now,
        subject=subject,
        text_payload=text,
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
    record.claimed_at = None
    record.claim_token = None
    session.commit()


def mark_digest_pending(
    session: Session,
    record: DigestRecord,
    error: Exception | str,
    now: datetime,
) -> None:
    now = _db_time(now)
    record.email_status = "pending"
    record.attempts += 1
    record.last_error_safe = safe_error(error)
    record.next_attempt_at = now + timedelta(minutes=_backoff_minutes(record.attempts))
    record.claimed_at = None
    record.claim_token = None
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
    now = _db_time(now)
    release_stale_digest_claims(session, now)
    candidates = list(
        session.scalars(
            select(DigestRecord.id)
            .where(DigestRecord.email_status == "pending")
            .where(DigestRecord.next_attempt_at <= now)
            .order_by(DigestRecord.created_at)
        )
    )
    sent = 0
    for record_id in candidates:
        token = uuid4().hex
        record = claim_pending_digest(session, record_id, now, token)
        if record:
            sent += int(send_claimed_digest(session, record.id, token, email_sender, now))
    return sent


def claim_pending_digest(
    session: Session,
    record_id: int,
    now: datetime,
    claim_token: str,
) -> DigestRecord | None:
    now = _db_time(now)
    result = session.execute(
        update(DigestRecord)
        .where(DigestRecord.id == record_id)
        .where(DigestRecord.email_status == "pending")
        .where(DigestRecord.next_attempt_at <= now)
        .values(email_status="sending", claimed_at=now, claim_token=claim_token)
    )
    session.commit()
    if result.rowcount != 1:
        return None
    return session.scalar(
        select(DigestRecord).where(
            DigestRecord.id == record_id,
            DigestRecord.claim_token == claim_token,
            DigestRecord.email_status == "sending",
        )
    )


def send_claimed_digest(
    session: Session,
    record_id: int,
    claim_token: str,
    email_sender,
    now: datetime,
) -> bool:
    record = session.scalar(
        select(DigestRecord).where(
            DigestRecord.id == record_id,
            DigestRecord.claim_token == claim_token,
            DigestRecord.email_status == "sending",
        )
    )
    if not record:
        return False
    try:
        email_sender.send(record.subject, record.text_payload, record.html_payload)
    except Exception as exc:
        mark_digest_pending(session, record, exc, now)
        return False
    mark_digest_sent(session, record)
    return True


def release_stale_digest_claims(session: Session, now: datetime, stale_minutes: int = 10) -> int:
    cutoff = _db_time(now) - timedelta(minutes=stale_minutes)
    result = session.execute(
        update(DigestRecord)
        .where(DigestRecord.email_status == "sending")
        .where(DigestRecord.claimed_at <= cutoff)
        .values(email_status="pending", claimed_at=None, claim_token=None)
    )
    session.commit()
    return int(result.rowcount or 0)


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
    now = _db_time(now)
    job = AlertJob(
        chat_id=chat_id,
        message_id=message_id,
        alert_type=alert_type,
        subject=subject,
        text_body=text_body,
        html_body=html_body,
        status="pending",
        attempts=0,
        next_attempt_at=now,
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


def claim_pending_alert(
    session: Session,
    job_id: int,
    now: datetime,
    claim_token: str,
) -> AlertJob | None:
    now = _db_time(now)
    result = session.execute(
        update(AlertJob)
        .where(AlertJob.id == job_id)
        .where(AlertJob.status == "pending")
        .where(AlertJob.next_attempt_at <= now)
        .values(status="sending", claimed_at=now, claim_token=claim_token)
    )
    session.commit()
    if result.rowcount != 1:
        return None
    return session.scalar(
        select(AlertJob).where(
            AlertJob.id == job_id,
            AlertJob.claim_token == claim_token,
            AlertJob.status == "sending",
        )
    )


def _mark_alert_pending(
    session: Session,
    job: AlertJob,
    error: Exception | str,
    now: datetime,
) -> None:
    now = _db_time(now)
    job.status = "pending"
    job.attempts += 1
    job.last_error_safe = safe_error(error)
    job.next_attempt_at = now + timedelta(minutes=_backoff_minutes(job.attempts))
    job.claimed_at = None
    job.claim_token = None
    session.commit()


def _mark_alert_sent(session: Session, job: AlertJob, now: datetime) -> None:
    job.status = "sent"
    job.sent_at = _db_time(now)
    job.last_error_safe = None
    job.next_attempt_at = None
    job.claimed_at = None
    job.claim_token = None
    session.commit()


def send_alert_job(session: Session, job: AlertJob, email_sender, now: datetime) -> bool:
    if job.status == "sent":
        return False
    token = uuid4().hex
    claimed = claim_pending_alert(session, job.id, job.next_attempt_at or now, token)
    if not claimed:
        return False
    return send_claimed_alert(session, claimed.id, token, email_sender, now)


def send_claimed_alert(
    session: Session,
    job_id: int,
    claim_token: str,
    email_sender,
    now: datetime,
) -> bool:
    job = session.scalar(
        select(AlertJob).where(
            AlertJob.id == job_id,
            AlertJob.claim_token == claim_token,
            AlertJob.status == "sending",
        )
    )
    if not job:
        return False
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
            _mark_alert_pending(session, job, exc, now)
            return False
    except Exception as exc:
        _mark_alert_pending(session, job, exc, now)
        return False
    _mark_alert_sent(session, job, now)
    return True


def retry_pending_alerts(session: Session, email_sender, now: datetime) -> int:
    now = _db_time(now)
    release_stale_alert_claims(session, now)
    job_ids = list(
        session.scalars(
            select(AlertJob.id)
            .where(AlertJob.status == "pending")
            .where(AlertJob.next_attempt_at <= now)
            .order_by(AlertJob.created_at, AlertJob.id)
        )
    )
    sent = 0
    for job_id in job_ids:
        token = uuid4().hex
        job = claim_pending_alert(session, job_id, now, token)
        if job:
            sent += int(send_claimed_alert(session, job.id, token, email_sender, now))
    return sent


def release_stale_alert_claims(session: Session, now: datetime, stale_minutes: int = 10) -> int:
    cutoff = _db_time(now) - timedelta(minutes=stale_minutes)
    result = session.execute(
        update(AlertJob)
        .where(AlertJob.status == "sending")
        .where(AlertJob.claimed_at <= cutoff)
        .values(status="pending", claimed_at=None, claim_token=None)
    )
    session.commit()
    return int(result.rowcount or 0)


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
