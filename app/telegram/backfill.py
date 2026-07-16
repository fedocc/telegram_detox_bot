from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.config import Settings
from app.db import repository
from app.email.sender import EmailSender
from app.llm.client import HaikuClient
from app.models.schemas import ChatType, MediaType, StoredMessage
from app.services.p0 import handle_p0_candidate
from app.services.prefilter import is_p0_candidate, is_urgent_call_candidate
from app.telegram.mapper import telegram_message_to_stored_message

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class BackfillStats:
    chats_scanned: int = 0
    messages_fetched: int = 0
    messages_inserted: int = 0
    duplicates_skipped: int = 0
    p0_classifications_triggered: int = 0


def _message_date(message) -> datetime:
    value = message.date or datetime.now().astimezone()
    return value.astimezone() if value.tzinfo else value.astimezone()


async def _message_sender(message):
    if hasattr(message, "get_sender"):
        return await message.get_sender()
    return getattr(message, "sender", None)


def _should_mark_old_review(message: StoredMessage) -> bool:
    if message.media_type != MediaType.none and not (message.text or message.caption):
        return True
    text = message.text or message.caption
    if message.chat_type == ChatType.private:
        return is_p0_candidate(text) or is_urgent_call_candidate(text)
    return is_p0_candidate(text) or is_urgent_call_candidate(text)


def _should_run_immediate_p0(
    message: StoredMessage,
    *,
    now: datetime,
    immediate_window_minutes: int,
) -> bool:
    age = now - message.timestamp
    return age <= timedelta(minutes=immediate_window_minutes)


async def run_startup_backfill(
    *,
    client,
    settings: Settings,
    session_factory,
    llm: HaikuClient,
    email_sender: EmailSender,
    now: datetime | None = None,
) -> BackfillStats:
    stats = BackfillStats()
    if not settings.backfill_enabled:
        return stats

    now = now or datetime.now().astimezone()
    fallback_since = now - timedelta(hours=settings.backfill_hours)
    remaining = settings.backfill_max_total_messages

    async for dialog in client.iter_dialogs():
        if remaining <= 0:
            break
        stats.chats_scanned += 1
        entity = dialog.entity
        chat_id = str(dialog.id)
        with session_factory() as session:
            latest = repository.latest_message_for_chat(session, chat_id)
            latest_id = latest.message_id if latest else None

        per_chat_limit = min(settings.backfill_max_messages_per_chat, remaining)
        async for tg_message in client.iter_messages(entity, limit=per_chat_limit):
            if remaining <= 0:
                break
            message_id = int(tg_message.id)
            if latest_id is not None and message_id <= latest_id:
                break
            timestamp = _message_date(tg_message)
            if latest_id is None and timestamp < fallback_since:
                break
            sender = await _message_sender(tg_message)
            stored = telegram_message_to_stored_message(
                tg_message,
                chat=entity,
                sender=sender,
                chat_id=chat_id,
                is_backfilled=True,
                ingested_at=now,
            )
            stats.messages_fetched += 1
            remaining -= 1
            with session_factory() as session:
                inserted = repository.insert_message_if_missing(session, stored)
                if not inserted:
                    stats.duplicates_skipped += 1
                    continue
                stats.messages_inserted += 1
                if _should_run_immediate_p0(
                    stored,
                    now=now,
                    immediate_window_minutes=settings.p0_backfill_immediate_window_minutes,
                ):
                    before = repository.get_message(
                        session,
                        stored.chat_id,
                        stored.message_id,
                    )
                    was_classified = bool(before and before.p0_classified_at)
                    handle_p0_candidate(session, stored, llm, email_sender, settings=settings)
                    after = repository.get_message(session, stored.chat_id, stored.message_id)
                    if after and after.p0_classified_at and not was_classified:
                        stats.p0_classifications_triggered += 1
                elif _should_mark_old_review(stored):
                    repository.mark_p0_review_candidate(session, stored.chat_id, stored.message_id)

    logger.info(
        "Telegram backfill complete: chats=%s fetched=%s inserted=%s duplicates=%s p0=%s",
        stats.chats_scanned,
        stats.messages_fetched,
        stats.messages_inserted,
        stats.duplicates_skipped,
        stats.p0_classifications_triggered,
    )
    return stats
