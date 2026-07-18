from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class ChatType(StrEnum):
    private = "private"
    group = "group"
    supergroup = "supergroup"
    channel = "channel"


class MediaType(StrEnum):
    none = "none"
    photo = "photo"
    voice = "voice"
    video = "video"
    document = "document"
    other = "other"


class Priority(StrEnum):
    p0 = "P0"
    p1 = "P1"
    p2 = "P2"
    p3 = "P3"
    review = "REVIEW"


class P0Status(StrEnum):
    p0_strict = "P0_STRICT"
    p0_candidate = "P0_CANDIDATE"
    not_p0 = "NOT_P0"

    # Source-level aliases keep callers compatible while persisted/provider values use
    # the explicit three-state policy above.
    p0 = "P0_STRICT"
    review = "P0_CANDIDATE"


P0_MIN_CONFIDENCE = 0.85


class MessageRef(BaseModel):
    chat_id: str
    message_id: int

    def __eq__(self, other: object) -> bool:
        if isinstance(other, dict):
            return self.model_dump() == other
        return super().__eq__(other)

    def values(self):
        return self.model_dump().values()


class StoredMessage(BaseModel):
    chat_id: str
    chat_title: str
    chat_type: ChatType
    sender_id: str | None = None
    sender_name: str | None = None
    message_id: int
    timestamp: datetime
    is_outgoing: bool = False
    reply_to_message_id: int | None = None
    text: str | None = None
    media_type: MediaType = MediaType.none
    caption: str | None = None
    alert_sent: bool = False
    is_backfilled: bool = False
    ingested_at: datetime | None = None


class P0Decision(BaseModel):
    status: P0Status = P0Status.p0_candidate
    summary: str
    reason: str | None = None
    action: str | None = None
    deadline_text: str | None = None
    deadline_at: datetime | None = None
    confidence: float = Field(ge=0, le=1)

    @property
    def is_p0(self) -> bool:
        return self.status == P0Status.p0_strict


class DigestP0Alert(BaseModel):
    chat: str
    sender: str | None = None
    summary: str
    action: str | None = None
    deadline_text: str | None = None
    deadline_at: datetime | None = None
    source_refs: list[MessageRef] = Field(default_factory=list)
    alert_sent: bool
    message_count: int | None = None
    first_message_at: datetime | None = None
    last_message_at: datetime | None = None


class DigestDirectMessage(BaseModel):
    chat: str
    summary: str
    needs_reply: bool
    action: str | None = None
    what_happened: str | None = None
    requests_to_me: str | None = None
    important_context: str | None = None
    action_items: str | None = None
    should_open_telegram: bool | None = None
    open_reason: str | None = None
    media_summary: str | None = None
    requests: str | None = None
    context: str | None = None
    open_telegram: bool = False
    open_telegram_reason: str | None = None
    deadline_text: str | None = None
    deadline_at: datetime | None = None
    priority: Priority = Priority.p1
    source_refs: list[MessageRef] = Field(default_factory=list)
    needs_manual_review: bool = False
    message_count: int | None = None
    first_message_at: datetime | None = None
    last_message_at: datetime | None = None


class DigestGroupUpdate(BaseModel):
    chat: str
    summary: str
    action: str | None = None
    what_happened: str | None = None
    requests_to_me: str | None = None
    important_context: str | None = None
    action_items: str | None = None
    should_open_telegram: bool | None = None
    open_reason: str | None = None
    media_summary: str | None = None
    requests: str | None = None
    context: str | None = None
    open_telegram: bool = False
    open_telegram_reason: str | None = None
    priority: Priority = Priority.p2
    deadline_text: str | None = None
    deadline_at: datetime | None = None
    source_refs: list[MessageRef] = Field(default_factory=list)
    needs_manual_review: bool = False
    message_count: int | None = None
    first_message_at: datetime | None = None
    last_message_at: datetime | None = None


class DigestReviewItem(BaseModel):
    chat: str
    reason: str
    summary: str
    source_refs: list[MessageRef] = Field(default_factory=list)
    sender: str | None = None
    timestamp: datetime | None = None
    raw_text: str | None = None


class DigestNoiseCount(BaseModel):
    chat: str
    count: int = Field(ge=0)


class DailyDigest(BaseModel):
    model_config = ConfigDict(use_enum_values=True)

    date: str
    p0_alerts: list[DigestP0Alert] = Field(default_factory=list)
    direct_messages: list[DigestDirectMessage] = Field(default_factory=list)
    group_updates: list[DigestGroupUpdate] = Field(default_factory=list)
    review: list[DigestReviewItem] = Field(default_factory=list)
    noise_counts: list[DigestNoiseCount] = Field(default_factory=list)
    generated_by: str = "llm"
    email_status: str = "pending"
    error_summary: str | None = None
