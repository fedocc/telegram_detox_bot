from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.db import repository
from app.email.render import digest_subject, render_html, render_plain_text
from app.email.sender import EmailSender
from app.llm.client import HaikuClient
from app.models.schemas import (
    ChatType,
    DailyDigest,
    DigestDirectMessage,
    DigestNoiseCount,
    DigestReviewItem,
)

APPROX_TOKEN_CHARS = 4
MAX_INPUT_TOKENS = 150_000


def day_bounds(day: date, timezone: str) -> tuple[datetime, datetime]:
    tz = ZoneInfo(timezone)
    start = datetime.combine(day, time.min, tzinfo=tz)
    end = datetime.combine(day, time.max, tzinfo=tz)
    return start, end


def build_structured_payload(rows: list) -> dict:
    chats: dict[str, dict] = {}
    for row in rows:
        chat = chats.setdefault(
            row.chat_id,
            {
                "chat_id": row.chat_id,
                "chat_title": row.chat_title,
                "chat_type": row.chat_type,
                "messages": [],
            },
        )
        chat["messages"].append(
            {
                "message_id": row.message_id,
                "timestamp": row.timestamp.isoformat(),
                "sender_name": row.sender_name,
                "is_outgoing": row.is_outgoing,
                "reply_to_message_id": row.reply_to_message_id,
                "text": row.text or row.caption,
                "media_type": row.media_type,
                "alert_sent": row.alert_sent,
            }
        )
    return {"chats": list(chats.values())}


def _estimated_tokens(payload: dict) -> int:
    return len(str(payload)) // APPROX_TOKEN_CHARS


def _merge_digests(day: date, digests: list[DailyDigest]) -> DailyDigest:
    merged = DailyDigest(date=day.isoformat())
    for digest in digests:
        merged.p0_alerts.extend(digest.p0_alerts)
        merged.direct_messages.extend(digest.direct_messages)
        merged.group_updates.extend(digest.group_updates)
        merged.review.extend(digest.review)
        merged.noise_counts.extend(digest.noise_counts)
    return merged


def _chat_chunks(chats: list[dict]) -> list[list[dict]]:
    chunks: list[list[dict]] = []
    current: list[dict] = []
    for chat in chats:
        candidate = [*current, chat]
        if current and _estimated_tokens({"chats": candidate}) > MAX_INPUT_TOKENS:
            chunks.append(current)
            current = [chat]
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def fallback_digest(day: date, rows: list) -> DailyDigest:
    direct: list[DigestDirectMessage] = []
    review: list[DigestReviewItem] = []
    noise_counts: list[DigestNoiseCount] = []
    grouped = defaultdict(list)
    for row in rows:
        grouped[row.chat_title].append(row)
        if row.media_type != "none" and not (row.text or row.caption):
            review.append(
                DigestReviewItem(
                    chat=row.chat_title,
                    reason=f"Необработанное медиа: {row.media_type}",
                    summary="Содержимое не анализировалось.",
                    message_ids=[row.message_id],
                )
            )
    for chat, chat_rows in grouped.items():
        incoming = [r for r in chat_rows if not r.is_outgoing]
        if incoming and incoming[0].chat_type == ChatType.private:
            direct.append(
                DigestDirectMessage(
                    chat=chat,
                    summary=f"{len(incoming)} входящих личных сообщений.",
                    needs_reply=True,
                    action="Проверить чат.",
                    message_ids=[r.message_id for r in incoming],
                    needs_manual_review=True,
                )
            )
        else:
            noise_counts.append(DigestNoiseCount(chat=chat, count=len(chat_rows)))
    return DailyDigest(
        date=day.isoformat(),
        direct_messages=direct,
        review=review,
        noise_counts=noise_counts,
    )


def generate_digest(session: Session, llm: HaikuClient, day: date, timezone: str) -> DailyDigest:
    start, end = day_bounds(day, timezone)
    rows = repository.messages_between(session, start, end)
    payload = build_structured_payload(rows)
    if _estimated_tokens(payload) <= MAX_INPUT_TOKENS:
        digest = llm.daily_digest({"date": day.isoformat(), **payload})
    else:
        chunk_digests = []
        for chunk in _chat_chunks(payload["chats"]):
            chunk_payload = {"date": day.isoformat(), "chats": chunk}
            if _estimated_tokens(chunk_payload) > MAX_INPUT_TOKENS:
                chat_ids = {chat["chat_id"] for chat in chunk}
                chunk_rows = [row for row in rows if row.chat_id in chat_ids]
                chunk_digests.append(fallback_digest(day, chunk_rows))
            else:
                chunk_digests.append(llm.daily_digest(chunk_payload))
        digest = _merge_digests(day, chunk_digests)
    for row in rows:
        if row.media_type != "none" and not (row.text or row.caption):
            digest.review.append(
                DigestReviewItem(
                    chat=row.chat_title,
                    reason=f"Необработанное медиа: {row.media_type}",
                    summary="Содержимое не анализировалось.",
                    message_ids=[row.message_id],
                )
            )
    return digest


def send_and_store_digest(session: Session, digest: DailyDigest, email_sender: EmailSender) -> None:
    html = render_html(digest)
    email_sender.send(digest_subject(digest), render_plain_text(digest), html)
    repository.save_digest(session, digest, html)
