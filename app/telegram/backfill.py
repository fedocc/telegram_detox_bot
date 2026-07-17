from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.config import Settings
from app.db import repository
from app.email.sender import EmailSender
from app.llm.client import HaikuClient
from app.models.schemas import ChatType, MediaType, StoredMessage
from app.services.p0 import handle_p0_candidate
from app.services.prefilter import is_p0_candidate, is_urgent_call_candidate
from app.telegram.mapper import chat_type, display_name, telegram_message_to_stored_message

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class BackfillStats:
    chats_scanned: int = 0
    messages_fetched: int = 0
    messages_inserted: int = 0
    duplicates_skipped: int = 0
    p0_classifications_triggered: int = 0


def _message_date(message) -> datetime:
    value = message.date or datetime.now(UTC)
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


async def _message_sender(message):
    if hasattr(message, "get_sender"):
        return await message.get_sender()
    return getattr(message, "sender", None)


def _state_time(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


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

    now = now or datetime.now(UTC)
    now = now.astimezone(UTC) if now.tzinfo else now.replace(tzinfo=UTC)
    fallback_since = now - timedelta(hours=settings.backfill_hours)
    remaining = settings.backfill_max_total_messages
    entities_by_chat_id = {}
    processed_this_run: dict[str, int] = {}

    async for dialog in client.iter_dialogs():
        stats.chats_scanned += 1
        entity = dialog.entity
        chat_id = str(dialog.id)
        entities_by_chat_id[chat_id] = entity
        with session_factory() as session:
            latest = repository.latest_message_for_chat(session, chat_id)
            repository.ensure_backfill_state(
                session,
                chat_id=chat_id,
                chat_title=display_name(entity),
                chat_type=chat_type(entity).value,
                window_start_utc=fallback_since,
                window_end_utc=now,
                last_processed_message_id=latest.message_id if latest else None,
            )

    while remaining > 0:
        progressed = False
        with session_factory() as session:
            states = repository.pending_backfill_states(session)
        for state in states:
            if remaining <= 0:
                break
            if processed_this_run.get(state.chat_id, 0) >= settings.backfill_max_messages_per_chat:
                continue
            entity = entities_by_chat_id.get(state.chat_id)
            if entity is None:
                continue
            iter_kwargs = {
                "limit": 1,
                "reverse": True,
                "offset_date": _state_time(state.window_start_utc),
            }
            if state.last_processed_message_id is not None:
                iter_kwargs["min_id"] = state.last_processed_message_id
            tg_message = None
            async for candidate in client.iter_messages(entity, **iter_kwargs):
                candidate_time = _message_date(candidate)
                if candidate_time < _state_time(state.window_start_utc):
                    continue
                if candidate_time > _state_time(state.window_end_utc):
                    with session_factory() as session:
                        fresh = session.merge(state)
                        repository.advance_backfill_state(
                            session,
                            fresh,
                            last_processed_message_id=fresh.last_processed_message_id,
                            completed=True,
                            increment_processed=False,
                        )
                    tg_message = None
                    break
                tg_message = candidate
                break
            if tg_message is None:
                with session_factory() as session:
                    fresh = session.merge(state)
                    repository.advance_backfill_state(
                        session,
                        fresh,
                        last_processed_message_id=fresh.last_processed_message_id,
                        completed=True,
                        increment_processed=False,
                    )
                continue
            sender = await _message_sender(tg_message)
            stored = telegram_message_to_stored_message(
                tg_message,
                chat=entity,
                sender=sender,
                chat_id=state.chat_id,
                is_backfilled=True,
                ingested_at=now,
            )
            stats.messages_fetched += 1
            remaining -= 1
            processed_this_run[state.chat_id] = processed_this_run.get(state.chat_id, 0) + 1
            progressed = True
            with session_factory() as session:
                inserted = repository.insert_message_if_missing(session, stored)
                if not inserted:
                    stats.duplicates_skipped += 1
                else:
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
                        repository.mark_p0_review_candidate(
                            session,
                            stored.chat_id,
                            stored.message_id,
                        )
                fresh = repository.ensure_backfill_state(
                    session,
                    chat_id=state.chat_id,
                    chat_title=state.chat_title,
                    chat_type=state.chat_type,
                    window_start_utc=_state_time(state.window_start_utc),
                    window_end_utc=_state_time(state.window_end_utc),
                    last_processed_message_id=state.last_processed_message_id,
                )
                repository.advance_backfill_state(
                    session,
                    fresh,
                    last_processed_message_id=stored.message_id,
                    completed=False,
                    increment_processed=True,
                )
        if not progressed:
            break

    logger.info(
        "Telegram backfill complete: chats=%s fetched=%s inserted=%s duplicates=%s p0=%s",
        stats.chats_scanned,
        stats.messages_fetched,
        stats.messages_inserted,
        stats.duplicates_skipped,
        stats.p0_classifications_triggered,
    )
    return stats
