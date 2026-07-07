from __future__ import annotations

import json
from datetime import datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.db.tables import DigestRecord, MessageRecord
from app.models.schemas import DailyDigest, StoredMessage


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


def save_digest(session: Session, digest: DailyDigest, html: str) -> None:
    session.add(
        DigestRecord(
            digest_date=digest.date,
            created_at=datetime.now().astimezone(),
            json_payload=digest.model_dump_json(),
            html_payload=html,
        )
    )
    session.commit()


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
