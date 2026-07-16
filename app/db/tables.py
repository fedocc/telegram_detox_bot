from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, Text, UniqueConstraint
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
    p0_classification: Mapped[str | None] = mapped_column(String(32), nullable=True)


class DigestRecord(Base):
    __tablename__ = "digests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    digest_date: Mapped[str] = mapped_column(String(16), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    subject: Mapped[str] = mapped_column(String(512), default="")
    text_payload: Mapped[str] = mapped_column(Text, default="")
    json_payload: Mapped[str] = mapped_column(Text)
    html_payload: Mapped[str] = mapped_column(Text)
    generated_by: Mapped[str] = mapped_column(String(32), default="llm")
    email_status: Mapped[str] = mapped_column(String(32), default="pending")
    error_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error_safe: Mapped[str | None] = mapped_column(Text, nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    claim_token: Mapped[str | None] = mapped_column(String(64), nullable=True)


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
