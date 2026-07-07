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


class P0Decision(BaseModel):
    is_p0: bool
    summary: str
    action: str | None = None
    deadline: datetime | None = None
    confidence: float = Field(ge=0, le=1)


class DigestP0Alert(BaseModel):
    chat: str
    sender: str | None = None
    summary: str
    action: str | None = None
    deadline: datetime | None = None
    message_ids: list[int]
    alert_sent: bool


class DigestDirectMessage(BaseModel):
    chat: str
    summary: str
    needs_reply: bool
    action: str | None = None
    deadline: datetime | None = None
    priority: Priority = Priority.p1
    message_ids: list[int]
    needs_manual_review: bool = False


class DigestGroupUpdate(BaseModel):
    chat: str
    summary: str
    action: str | None = None
    priority: Priority = Priority.p2
    deadline: datetime | None = None
    message_ids: list[int]
    needs_manual_review: bool = False


class DigestReviewItem(BaseModel):
    chat: str
    reason: str
    summary: str
    message_ids: list[int]


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

