from __future__ import annotations

from datetime import datetime

from app.models.schemas import ChatType, MediaType, StoredMessage


def msg(
    *,
    chat_id: str = "1",
    chat_title: str = "Маша",
    chat_type: ChatType = ChatType.private,
    message_id: int = 1,
    text: str | None = "Привет, сможешь завтра встретиться?",
    is_outgoing: bool = False,
    media_type: MediaType = MediaType.none,
    timestamp: datetime | None = None,
) -> StoredMessage:
    return StoredMessage(
        chat_id=chat_id,
        chat_title=chat_title,
        chat_type=chat_type,
        sender_id="42",
        sender_name="Sender",
        message_id=message_id,
        timestamp=timestamp or datetime.fromisoformat("2026-07-07T12:00:00+03:00"),
        is_outgoing=is_outgoing,
        text=text if media_type == MediaType.none else None,
        media_type=media_type,
        caption=text if media_type != MediaType.none else None,
    )

