from __future__ import annotations

from datetime import UTC, datetime

from telethon.tl.types import Channel, Chat, MessageMediaDocument, MessageMediaPhoto, User

from app.models.schemas import ChatType, MediaType, StoredMessage


def chat_type(entity) -> ChatType:
    if isinstance(entity, User):
        return ChatType.private
    if isinstance(entity, Channel):
        return ChatType.supergroup if getattr(entity, "megagroup", False) else ChatType.channel
    if isinstance(entity, Chat):
        return ChatType.group
    return ChatType.group


def display_name(entity) -> str:
    title = getattr(entity, "title", None)
    if title:
        return title
    first = getattr(entity, "first_name", "") or ""
    last = getattr(entity, "last_name", "") or ""
    username = getattr(entity, "username", "") or ""
    return (
        " ".join(part for part in [first, last] if part)
        or username
        or str(getattr(entity, "id", "unknown"))
    )


def media_type(message) -> MediaType:
    media = getattr(message, "media", None)
    if media is None:
        return MediaType.none
    if isinstance(media, MessageMediaPhoto):
        return MediaType.photo
    if isinstance(media, MessageMediaDocument):
        document = getattr(media, "document", None)
        mime = getattr(document, "mime_type", "") or ""
        if mime.startswith("audio/"):
            return MediaType.voice
        if mime.startswith("video/"):
            return MediaType.video
        return MediaType.document
    return MediaType.other


def telegram_message_to_stored_message(
    msg,
    *,
    chat,
    sender,
    chat_id: str,
    is_backfilled: bool = False,
    ingested_at: datetime | None = None,
) -> StoredMessage:
    text = msg.raw_text or None
    mtype = media_type(msg)
    return StoredMessage(
        chat_id=chat_id,
        chat_title=display_name(chat),
        chat_type=chat_type(chat),
        sender_id=str(getattr(sender, "id", "")) if sender else None,
        sender_name=display_name(sender) if sender else None,
        message_id=msg.id,
        timestamp=(msg.date or datetime.now(UTC)).astimezone(UTC),
        is_outgoing=bool(msg.out),
        reply_to_message_id=getattr(msg, "reply_to_msg_id", None),
        text=text if mtype == MediaType.none else None,
        media_type=mtype,
        caption=text if mtype != MediaType.none else None,
        is_backfilled=is_backfilled,
        ingested_at=ingested_at,
    )


async def event_to_stored_message(event) -> StoredMessage:
    msg = event.message
    chat = await event.get_chat()
    sender = await event.get_sender()
    return telegram_message_to_stored_message(
        msg,
        chat=chat,
        sender=sender,
        chat_id=str(event.chat_id),
        ingested_at=datetime.now(UTC),
    )
