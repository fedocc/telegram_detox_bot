from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class MessageRecord(Base):
    __tablename__ = "messages"
    __table_args__ = (UniqueConstraint("chat_id", "message_id", name="uq_chat_message"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[str] = mapped_column(String(128), index=True)
    chat_title: Mapped[str] = mapped_column(String(512))
    chat_type: Mapped[str] = mapped_column(String(32), index=True)
    sender_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    sender_name: Mapped[str | None] = mapped_column(String(512), nullable=True)
    message_id: Mapped[int] = mapped_column(Integer)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    is_outgoing: Mapped[bool] = mapped_column(Boolean, default=False)
    reply_to_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reply_to_is_mine: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    text: Mapped[str | None] = mapped_column(Text, nullable=True)
    media_type: Mapped[str] = mapped_column(String(32), default="none")
    caption: Mapped[str | None] = mapped_column(Text, nullable=True)
    alert_sent: Mapped[bool] = mapped_column(Boolean, default=False)
    is_backfilled: Mapped[bool] = mapped_column(Boolean, default=False)
    ingested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    p0_review_candidate: Mapped[bool] = mapped_column(Boolean, default=False)
    digested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    raw_redacted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    p0_classified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    p0_llm_called_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    p0_classification: Mapped[str | None] = mapped_column(String(32), nullable=True)
    p0_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    claimed_digest_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)


class DigestRecord(Base):
    __tablename__ = "digests"
    __table_args__ = (UniqueConstraint("digest_key", name="uq_digest_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    digest_date: Mapped[str] = mapped_column(String(16), index=True)
    digest_key: Mapped[str | None] = mapped_column(String(256), nullable=True)
    delivery_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    subject: Mapped[str] = mapped_column(String(512), default="")
    text_payload: Mapped[str] = mapped_column(Text, default="")
    json_payload: Mapped[str] = mapped_column(Text)
    source_chat_ids: Mapped[str | None] = mapped_column(Text, nullable=True)
    html_payload: Mapped[str] = mapped_column(Text)
    generated_by: Mapped[str] = mapped_column(String(32), default="llm")
    email_status: Mapped[str] = mapped_column(String(32), default="pending")
    error_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error_safe: Mapped[str | None] = mapped_column(Text, nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    claim_token: Mapped[str | None] = mapped_column(String(64), nullable=True)


class BackfillState(Base):
    __tablename__ = "backfill_states"
    __table_args__ = (UniqueConstraint("chat_id", name="uq_backfill_state_chat"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[str] = mapped_column(String(128), index=True)
    chat_title: Mapped[str] = mapped_column(String(512))
    chat_type: Mapped[str] = mapped_column(String(32), default="group")
    window_start_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    window_end_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completed: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    last_processed_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    messages_processed: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class BirthdayContact(Base):
    __tablename__ = "birthday_contacts"
    __table_args__ = (UniqueConstraint("person_key", name="uq_birthday_contact_person"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    person_key: Mapped[str] = mapped_column(String(128), index=True)
    telegram_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    display_name_safe: Mapped[str] = mapped_column(String(512))
    username: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    day: Mapped[int] = mapped_column(Integer)
    month: Mapped[int] = mapped_column(Integer)
    year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source: Mapped[str] = mapped_column(String(32))
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class BirthdayNotification(Base):
    __tablename__ = "birthday_notifications"
    __table_args__ = (
        UniqueConstraint(
            "person_key",
            "birthday_date",
            "notification_type",
            name="uq_birthday_notification_person_date_type",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    person_key: Mapped[str] = mapped_column(String(128), index=True)
    birthday_date: Mapped[date] = mapped_column(Date, index=True)
    notification_type: Mapped[str] = mapped_column(String(16), index=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AlertJob(Base):
    __tablename__ = "alert_jobs"
    __table_args__ = (
        UniqueConstraint("chat_id", "message_id", "alert_type", name="uq_alert_job_message_type"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[str] = mapped_column(String(128), index=True)
    message_id: Mapped[int] = mapped_column(Integer)
    alert_type: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    subject: Mapped[str] = mapped_column(String(512))
    html_body: Mapped[str] = mapped_column(Text)
    text_body: Mapped[str] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error_safe: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    claim_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
