from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import uuid4

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.db.tables import AlertJob, BackfillState, DigestRecord, MessageRecord
from app.models.schemas import DailyDigest, StoredMessage


def _backoff_minutes(attempts: int) -> int:
    return [1, 5, 15, 60][min(max(attempts - 1, 0), 3)]


def safe_error(exc: Exception | str | None) -> str | None:
    if exc is None:
        return None
    return exc if isinstance(exc, str) else exc.__class__.__name__


def _db_time(value: datetime) -> datetime:
    return value.replace(tzinfo=None) if value.tzinfo else value


def _utc_db_time(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def save_message(session: Session, message: StoredMessage) -> None:
    values = message.model_dump(mode="python")
    values["chat_type"] = message.chat_type.value
    values["media_type"] = message.media_type.value
    values["timestamp"] = _utc_db_time(values["timestamp"])
    if values.get("ingested_at") is not None:
        values["ingested_at"] = _utc_db_time(values["ingested_at"])
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


def insert_message_if_missing(session: Session, message: StoredMessage) -> bool:
    existing = get_message(session, message.chat_id, message.message_id)
    if existing:
        return False
    save_message(session, message)
    return True


def get_message(session: Session, chat_id: str, message_id: int) -> MessageRecord | None:
    return session.scalar(
        select(MessageRecord).where(
            MessageRecord.chat_id == chat_id,
            MessageRecord.message_id == message_id,
        )
    )


def latest_message_for_chat(session: Session, chat_id: str) -> MessageRecord | None:
    return session.scalar(
        select(MessageRecord)
        .where(MessageRecord.chat_id == chat_id)
        .order_by(MessageRecord.message_id.desc())
        .limit(1)
    )


def ensure_backfill_state(
    session: Session,
    *,
    chat_id: str,
    chat_title: str,
    chat_type: str,
    window_start_utc: datetime,
    window_end_utc: datetime,
    last_processed_message_id: int | None,
) -> BackfillState:
    existing = session.scalar(select(BackfillState).where(BackfillState.chat_id == chat_id))
    if existing:
        return existing
    now = _utc_db_time(datetime.now(UTC))
    state = BackfillState(
        chat_id=chat_id,
        chat_title=chat_title,
        chat_type=chat_type,
        window_start_utc=_utc_db_time(window_start_utc),
        window_end_utc=_utc_db_time(window_end_utc),
        completed=False,
        last_processed_message_id=last_processed_message_id,
        messages_processed=0,
        created_at=now,
        updated_at=now,
    )
    session.add(state)
    session.commit()
    return state


def pending_backfill_states(session: Session) -> list[BackfillState]:
    return list(
        session.scalars(
            select(BackfillState)
            .where(BackfillState.completed.is_(False))
            .order_by(BackfillState.updated_at, BackfillState.id)
        )
    )


def advance_backfill_state(
    session: Session,
    state: BackfillState,
    *,
    last_processed_message_id: int | None,
    completed: bool,
    increment_processed: bool,
) -> None:
    if last_processed_message_id is not None:
        state.last_processed_message_id = last_processed_message_id
    state.completed = completed
    if increment_processed:
        state.messages_processed += 1
    state.updated_at = _utc_db_time(datetime.now(UTC))
    session.commit()


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


def mark_p0_classified(
    session: Session,
    chat_id: str,
    message_id: int,
    status: str,
    classified_at: datetime,
) -> None:
    record = get_message(session, chat_id, message_id)
    if record:
        record.p0_classified_at = _utc_db_time(classified_at)
        record.p0_classification = status
        session.commit()


def p0_llm_calls_since(session: Session, since: datetime) -> int:
    return int(
        session.scalar(
            select(func.count())
            .select_from(MessageRecord)
            .where(MessageRecord.p0_classified_at >= _utc_db_time(since))
        )
        or 0
    )


def recent_chat_context(session: Session, chat_id: str, limit: int = 10) -> list[MessageRecord]:
    rows = session.scalars(
        select(MessageRecord)
        .where(MessageRecord.chat_id == chat_id)
        .order_by(MessageRecord.timestamp.desc(), MessageRecord.message_id.desc())
        .limit(limit)
    ).all()
    return list(reversed(rows))


def messages_between(
    session: Session,
    start: datetime,
    end: datetime,
    *,
    only_undigested: bool = True,
    limit: int | None = None,
) -> list[MessageRecord]:
    start = _utc_db_time(start)
    end = _utc_db_time(end)
    stmt = (
        select(MessageRecord)
        .where(MessageRecord.timestamp >= start, MessageRecord.timestamp < end)
        .order_by(MessageRecord.chat_title, MessageRecord.timestamp, MessageRecord.message_id)
    )
    if only_undigested:
        stmt = stmt.where(MessageRecord.digested_at.is_(None))
        stmt = stmt.where(MessageRecord.claimed_digest_id.is_(None))
    if limit is not None:
        stmt = stmt.limit(limit)
    return list(session.scalars(stmt))


def messages_claimed_by_digest(session: Session, digest_id: int) -> list[MessageRecord]:
    return list(
        session.scalars(
            select(MessageRecord)
            .where(MessageRecord.claimed_digest_id == digest_id)
            .order_by(MessageRecord.chat_title, MessageRecord.timestamp, MessageRecord.message_id)
        )
    )


def mark_messages_digested(session: Session, rows: list, digested_at: datetime) -> int:
    refs = [(row.chat_id, row.message_id) for row in rows]
    if not refs:
        return 0
    count = 0
    digested_at = _utc_db_time(digested_at)
    for chat_id, message_id in refs:
        result = session.execute(
            update(MessageRecord)
            .where(MessageRecord.chat_id == chat_id)
            .where(MessageRecord.message_id == message_id)
            .values(digested_at=digested_at, claimed_digest_id=None)
        )
        count += int(result.rowcount or 0)
    session.commit()
    return count


def _digest_key_for_rows(digest_date: str, rows: list) -> str:
    refs = sorted((row.chat_id, row.message_id) for row in rows)
    material = json.dumps([digest_date, refs], ensure_ascii=True, separators=(",", ":"))
    return f"digest:{digest_date}:{sha256(material.encode('utf-8')).hexdigest()}"


def _delivery_id_for_key(digest_key: str) -> str:
    digest_hash = sha256(digest_key.encode("utf-8")).hexdigest()
    return f"<telegram-digest-{digest_hash}@local>"


def claim_digest_run_for_rows(
    session: Session,
    *,
    digest_date: str,
    rows: list[MessageRecord],
) -> tuple[DigestRecord | None, list[MessageRecord], bool]:
    if not rows:
        return None, [], False
    digest_key = _digest_key_for_rows(digest_date, rows)
    existing = session.scalar(select(DigestRecord).where(DigestRecord.digest_key == digest_key))
    if existing:
        return existing, messages_claimed_by_digest(session, existing.id), False

    now = _utc_db_time(datetime.now(UTC))
    record = DigestRecord(
        digest_date=digest_date,
        digest_key=digest_key,
        delivery_id=_delivery_id_for_key(digest_key),
        created_at=now,
        subject="",
        text_payload="",
        json_payload=DailyDigest(date=digest_date).model_dump_json(),
        html_payload="",
        generated_by="llm",
        email_status="building",
        attempts=0,
        next_attempt_at=None,
    )
    session.add(record)
    try:
        session.flush()
        claimed_refs: list[tuple[str, int]] = []
        for row in rows:
            result = session.execute(
                update(MessageRecord)
                .where(MessageRecord.chat_id == row.chat_id)
                .where(MessageRecord.message_id == row.message_id)
                .where(MessageRecord.digested_at.is_(None))
                .where(MessageRecord.claimed_digest_id.is_(None))
                .values(claimed_digest_id=record.id)
            )
            if result.rowcount:
                claimed_refs.append((row.chat_id, row.message_id))
        if not claimed_refs:
            session.rollback()
            return None, [], False
        session.commit()
    except Exception:
        session.rollback()
        existing = session.scalar(select(DigestRecord).where(DigestRecord.digest_key == digest_key))
        if existing:
            return existing, messages_claimed_by_digest(session, existing.id), False
        raise
    return record, messages_claimed_by_digest(session, record.id), True


def update_digest_payload(
    session: Session,
    record: DigestRecord,
    digest: DailyDigest,
    *,
    subject: str,
    text: str,
    html: str,
) -> None:
    if not subject.strip() or not text.strip() or not html.strip():
        raise ValueError("Digest payload must be non-empty before it can be sent")
    record.subject = subject
    record.text_payload = text
    record.html_payload = html
    record.json_payload = digest.model_dump_json()
    record.generated_by = digest.generated_by
    record.error_summary = digest.error_summary
    record.email_status = "pending"
    record.next_attempt_at = _utc_db_time(datetime.now(UTC))
    session.commit()


def _digest_is_sendable(record: DigestRecord) -> bool:
    return bool(
        record.email_status in {"pending", "sending"}
        and record.subject
        and record.text_payload
        and record.html_payload
        and record.json_payload
    )


def save_digest(
    session: Session,
    digest: DailyDigest,
    html: str,
    *,
    subject: str = "",
    text: str = "",
) -> DigestRecord:
    now = _utc_db_time(datetime.now(UTC))
    record = DigestRecord(
        digest_date=digest.date,
        digest_key=None,
        delivery_id=f"<telegram-digest-{uuid4().hex}@local>",
        created_at=now,
        subject=subject,
        text_payload=text,
        json_payload=digest.model_dump_json(),
        html_payload=html,
        generated_by=digest.generated_by,
        email_status=digest.email_status,
        error_summary=digest.error_summary,
        attempts=0,
        next_attempt_at=now if digest.email_status == "pending" else None,
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


def finalize_digest_sent(session: Session, record: DigestRecord, sent_at: datetime) -> None:
    sent_at = _utc_db_time(sent_at)
    record.email_status = "sent"
    record.last_error_safe = None
    record.next_attempt_at = None
    record.claimed_at = None
    record.claim_token = None
    session.execute(
        update(MessageRecord)
        .where(MessageRecord.claimed_digest_id == record.id)
        .values(digested_at=sent_at, claimed_digest_id=None)
    )
    session.commit()


def mark_digest_pending(
    session: Session,
    record: DigestRecord,
    error: Exception | str,
    now: datetime,
) -> None:
    now = _utc_db_time(now)
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


def pending_digest_for_date(session: Session, digest_date: str) -> DigestRecord | None:
    return session.scalar(
        select(DigestRecord)
        .where(DigestRecord.digest_date == digest_date)
        .where(DigestRecord.email_status.in_(["building", "pending", "sending"]))
        .order_by(DigestRecord.created_at.desc())
        .limit(1)
    )


def undigested_message_timestamps_before(session: Session, before: datetime) -> list[datetime]:
    before = _utc_db_time(before)
    return list(
        session.scalars(
            select(MessageRecord.timestamp)
            .where(MessageRecord.digested_at.is_(None))
            .where(MessageRecord.timestamp < before)
            .order_by(MessageRecord.timestamp)
        )
    )


def retry_pending_digests(session: Session, email_sender, now: datetime) -> int:
    now = _utc_db_time(now)
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
    now = _utc_db_time(now)
    result = session.execute(
        update(DigestRecord)
        .where(DigestRecord.id == record_id)
        .where(DigestRecord.email_status == "pending")
        .where(DigestRecord.next_attempt_at <= now)
        .where(DigestRecord.subject != "")
        .where(DigestRecord.text_payload != "")
        .where(DigestRecord.html_payload != "")
        .where(DigestRecord.json_payload != "")
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
    if not _digest_is_sendable(record):
        return False
    try:
        try:
            email_sender.send(
                record.subject,
                record.text_payload,
                record.html_payload,
                message_id=record.delivery_id,
            )
        except TypeError:
            email_sender.send(record.subject, record.text_payload, record.html_payload)
    except Exception as exc:
        mark_digest_pending(session, record, exc, now)
        return False
    finalize_digest_sent(session, record, now)
    return True


def release_stale_digest_claims(session: Session, now: datetime, stale_minutes: int = 10) -> int:
    cutoff = _utc_db_time(now) - timedelta(minutes=stale_minutes)
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
    now = _utc_db_time(now)
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
    now = _utc_db_time(now)
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
    job.sent_at = _utc_db_time(now)
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
    now = _utc_db_time(now)
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
    cutoff = _utc_db_time(now) - timedelta(minutes=stale_minutes)
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
