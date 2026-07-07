from __future__ import annotations

import time as time_module
from collections import defaultdict
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.db import repository
from app.email.render import digest_subject, render_html, render_plain_text
from app.email.sender import EmailSender, EmailSendError
from app.llm.client import HaikuClient, LLMError
from app.models.schemas import (
    ChatType,
    DailyDigest,
    DigestDirectMessage,
    DigestNoiseCount,
    DigestReviewItem,
    MessageRef,
)

APPROX_TOKEN_CHARS = 4
MAX_INPUT_TOKENS = 150_000
REVIEW_TEXT_LIMIT = 500


def safe_truncate(text: str | None, limit: int = REVIEW_TEXT_LIMIT) -> str:
    if not text:
        return ""
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 3] + "..."


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
                "source_ref": {"chat_id": row.chat_id, "message_id": row.message_id},
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
                    source_refs=[MessageRef(chat_id=row.chat_id, message_id=row.message_id)],
                )
            )
        if getattr(row, "p0_review_candidate", False):
            review.append(
                DigestReviewItem(
                    chat=row.chat_title,
                    reason="P0 review candidate",
                    summary=safe_truncate(row.text or row.caption or "Needs manual review."),
                    source_refs=[MessageRef(chat_id=row.chat_id, message_id=row.message_id)],
                    sender=row.sender_name,
                    timestamp=row.timestamp,
                    raw_text=safe_truncate(row.text or row.caption),
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
                    source_refs=[
                        MessageRef(chat_id=r.chat_id, message_id=r.message_id)
                        for r in incoming
                    ],
                    needs_manual_review=True,
                )
            )
            for row in incoming:
                review.append(
                    DigestReviewItem(
                        chat=row.chat_title,
                        reason="Fallback digest includes incoming private message",
                        summary=safe_truncate(
                            row.text or row.caption or "Личное сообщение без текста."
                        ),
                        source_refs=[MessageRef(chat_id=row.chat_id, message_id=row.message_id)],
                        sender=row.sender_name,
                        timestamp=row.timestamp,
                        raw_text=safe_truncate(row.text or row.caption),
                    )
                )
        else:
            noise_counts.append(DigestNoiseCount(chat=chat, count=len(chat_rows)))
    return DailyDigest(
        date=day.isoformat(),
        direct_messages=direct,
        review=review,
        noise_counts=noise_counts,
        generated_by="fallback",
    )


def _classified_refs(digest: DailyDigest) -> set[tuple[str, int]]:
    refs: set[tuple[str, int]] = set()
    for item in [*digest.direct_messages, *digest.review]:
        refs.update((ref.chat_id, ref.message_id) for ref in item.source_refs)
    return refs


def _protect_private_messages(digest: DailyDigest, rows: list) -> DailyDigest:
    classified = _classified_refs(digest)
    private_refs = {
        (row.chat_id, row.message_id)
        for row in rows
        if row.chat_type == ChatType.private and not row.is_outgoing
    }
    missing = private_refs - classified
    if missing:
        digest.noise_counts = [
            count
            for count in digest.noise_counts
            if not any(
                row.chat_title == count.chat
                and (row.chat_id, row.message_id) in private_refs
                and row.chat_type == ChatType.private
                for row in rows
            )
        ]
    for row in rows:
        if (row.chat_id, row.message_id) not in missing:
            continue
        digest.review.append(
            DigestReviewItem(
                chat=row.chat_title,
                reason="LLM did not classify this incoming private message",
                summary=safe_truncate(row.text or row.caption or "Личное сообщение без текста."),
                source_refs=[MessageRef(chat_id=row.chat_id, message_id=row.message_id)],
                sender=row.sender_name,
                timestamp=row.timestamp,
                raw_text=safe_truncate(row.text or row.caption),
            )
        )
    return digest


def _call_daily_digest(llm: HaikuClient, payload: dict, day: date, rows: list) -> DailyDigest:
    try:
        return llm.daily_digest(payload)
    except (LLMError, TimeoutError, RuntimeError, ValueError) as exc:
        digest = fallback_digest(day, rows)
        digest.error_summary = safe_truncate(str(exc), 200)
        return digest


def generate_digest(session: Session, llm: HaikuClient, day: date, timezone: str) -> DailyDigest:
    start, end = day_bounds(day, timezone)
    rows = repository.messages_between(session, start, end)
    payload = build_structured_payload(rows)
    if _estimated_tokens(payload) <= MAX_INPUT_TOKENS:
        digest = _call_daily_digest(llm, {"date": day.isoformat(), **payload}, day, rows)
    else:
        chunk_digests = []
        for chunk in _chat_chunks(payload["chats"]):
            chunk_payload = {"date": day.isoformat(), "chats": chunk}
            if _estimated_tokens(chunk_payload) > MAX_INPUT_TOKENS:
                chat_ids = {chat["chat_id"] for chat in chunk}
                chunk_rows = [row for row in rows if row.chat_id in chat_ids]
                chunk_digests.append(fallback_digest(day, chunk_rows))
            else:
                chat_ids = {chat["chat_id"] for chat in chunk}
                chunk_rows = [row for row in rows if row.chat_id in chat_ids]
                chunk_digests.append(_call_daily_digest(llm, chunk_payload, day, chunk_rows))
        digest = _merge_digests(day, chunk_digests)
    for row in rows:
        if row.media_type != "none" and not (row.text or row.caption):
            digest.review.append(
                DigestReviewItem(
                    chat=row.chat_title,
                    reason=f"Необработанное медиа: {row.media_type}",
                    summary="Содержимое не анализировалось.",
                    source_refs=[MessageRef(chat_id=row.chat_id, message_id=row.message_id)],
                )
            )
    return _protect_private_messages(digest, rows)


def send_and_store_digest(session: Session, digest: DailyDigest, email_sender: EmailSender) -> None:
    html = render_html(digest)
    text = render_plain_text(digest)
    subject = digest_subject(digest)
    digest.email_status = "pending"
    record = repository.save_digest(session, digest, html, subject=subject, text=text)
    try:
        email_sender.send(subject, text, html)
    except EmailSendError as exc:
        repository.mark_digest_pending(session, record, exc, datetime.now().astimezone())
    else:
        digest.email_status = "sent"
        repository.mark_digest_sent(session, record)


def _subject_for(digest: DailyDigest) -> str:
    if digest.generated_by == "fallback":
        return f"[FALLBACK] Telegram digest — {digest.date}"
    return digest_subject(digest)


def send_daily_digest_pipeline(
    session: Session,
    llm: HaikuClient,
    email_sender: EmailSender,
    day: date,
    timezone: str,
    *,
    max_email_attempts: int = 3,
) -> DailyDigest:
    digest = generate_digest(session, llm, day, timezone)
    html = render_html(digest)
    text = render_plain_text(digest)
    subject = _subject_for(digest)
    digest.email_status = "pending"
    record = repository.save_digest(session, digest, html, subject=subject, text=text)
    now = datetime.now().astimezone()
    for attempt in range(max_email_attempts):
        try:
            email_sender.send(subject, text, html)
            digest.email_status = "sent"
            repository.mark_digest_sent(session, record)
            return digest
        except EmailSendError as exc:
            repository.mark_digest_pending(session, record, exc, now)
            if attempt < max_email_attempts - 1:
                time_module.sleep(min(2**attempt, 8))
    digest.email_status = "pending"
    digest.error_summary = record.last_error_safe
    return digest
