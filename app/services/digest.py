from __future__ import annotations

from collections import defaultdict
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.db import repository
from app.email.render import digest_subject, render_html, render_plain_text
from app.email.sender import EmailSender
from app.llm.client import HaikuClient, LLMError
from app.models.schemas import (
    ChatType,
    DailyDigest,
    DigestDirectMessage,
    DigestGroupUpdate,
    DigestNoiseCount,
    DigestReviewItem,
    MessageRef,
)

APPROX_TOKEN_CHARS = 4
MAX_INPUT_TOKENS = 150_000
REVIEW_TEXT_LIMIT = 500
MAX_MESSAGES_PER_DIGEST_WINDOW = 5_000
MAX_MESSAGES_PER_CHAT = 100
MAX_CHARS_PER_GROUP = 12_000
BAD_SUMMARY_PHRASES = ("короткая переписка",)


def safe_truncate(text: str | None, limit: int = REVIEW_TEXT_LIMIT) -> str:
    if not text:
        return ""
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 3] + "..."


def _clean_summary(text: str | None) -> str:
    summary = safe_truncate(text or "обновление", 280)
    for phrase in BAD_SUMMARY_PHRASES:
        summary = summary.replace(phrase, "обсуждение")
    return summary


def _chat_type_value(row) -> str:
    value = row.chat_type
    return value.value if hasattr(value, "value") else str(value)


def day_bounds(day: date, timezone: str) -> tuple[datetime, datetime]:
    tz = ZoneInfo(timezone)
    start = datetime.combine(day, time.min, tzinfo=tz)
    end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=tz)
    return start.astimezone(UTC), end.astimezone(UTC)


def build_structured_payload(
    rows: list,
    *,
    max_messages_per_chat: int = MAX_MESSAGES_PER_CHAT,
    max_chars_per_group: int = MAX_CHARS_PER_GROUP,
) -> dict:
    chats: dict[str, dict] = {}
    overflow_notes: list[dict] = []
    for row in rows:
        chat = chats.setdefault(
            row.chat_id,
            {
                "chat_id": row.chat_id,
                "chat_title": row.chat_title,
                "chat_type": row.chat_type,
                "messages": [],
                "omitted_messages": 0,
                "omitted_chars": 0,
            },
        )
        current_chars = sum(len(str(message.get("text") or "")) for message in chat["messages"])
        text = safe_truncate(row.text or row.caption, REVIEW_TEXT_LIMIT)
        if len(chat["messages"]) >= max_messages_per_chat:
            chat["omitted_messages"] += 1
            continue
        if current_chars + len(text) > max_chars_per_group:
            chat["omitted_chars"] += len(text)
            continue
        chat["messages"].append(
            {
                "message_id": row.message_id,
                "source_ref": {"chat_id": row.chat_id, "message_id": row.message_id},
                "timestamp": row.timestamp.isoformat(),
                "sender_name": row.sender_name,
                "is_outgoing": row.is_outgoing,
                "reply_to_message_id": row.reply_to_message_id,
                "text": text,
                "media_type": row.media_type,
                "alert_sent": row.alert_sent,
            }
        )
    for chat in chats.values():
        if chat["omitted_messages"] or chat["omitted_chars"]:
            overflow_notes.append(
                {
                    "chat": chat["chat_title"],
                    "summary": (
                        "Часть сообщений не отправлена в LLM из-за лимита: "
                        f"{chat['omitted_messages']} сообщений, {chat['omitted_chars']} символов."
                    ),
                }
            )
    return {"chats": list(chats.values()), "overflow_notes": overflow_notes}


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
    group_updates: list[DigestGroupUpdate] = []
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
                    reason="Возможно важное сообщение",
                    summary=safe_truncate(row.text or row.caption or "Needs manual review."),
                    source_refs=[MessageRef(chat_id=row.chat_id, message_id=row.message_id)],
                    sender=row.sender_name,
                    timestamp=row.timestamp,
                    raw_text=safe_truncate(row.text or row.caption),
                )
            )
    for chat, chat_rows in grouped.items():
        incoming = [r for r in chat_rows if not r.is_outgoing]
        if incoming and _chat_type_value(incoming[0]) == ChatType.private.value:
            first = min(r.timestamp for r in incoming)
            last = max(r.timestamp for r in incoming)
            direct.append(
                DigestDirectMessage(
                    chat=chat,
                    summary=f"{len(incoming)} входящих личных сообщений.",
                    needs_reply=False,
                    action=None,
                    source_refs=[
                        MessageRef(chat_id=r.chat_id, message_id=r.message_id)
                        for r in incoming
                    ],
                    needs_manual_review=True,
                    message_count=len(incoming),
                    first_message_at=first,
                    last_message_at=last,
                )
            )
        else:
            first = min(r.timestamp for r in chat_rows)
            last = max(r.timestamp for r in chat_rows)
            summary = "; ".join(
                safe_truncate(r.text or r.caption, 80)
                for r in chat_rows[:3]
                if r.text or r.caption
            )
            direct_refs = [
                MessageRef(chat_id=r.chat_id, message_id=r.message_id)
                for r in chat_rows
            ]
            if chat_rows and _chat_type_value(chat_rows[0]) in {
                ChatType.group.value,
                ChatType.supergroup.value,
                ChatType.channel.value,
            }:
                # Use group updates for fallback digests so user sees one concise group line.
                group_updates.append(
                    DigestGroupUpdate(
                        chat=chat,
                        summary=_clean_summary(summary or f"{len(chat_rows)} сообщений."),
                        source_refs=direct_refs,
                        message_count=len(chat_rows),
                        first_message_at=first,
                        last_message_at=last,
                    )
                )
            else:
                noise_counts.append(DigestNoiseCount(chat=chat, count=len(chat_rows)))
    return DailyDigest(
        date=day.isoformat(),
        direct_messages=direct,
        group_updates=group_updates,
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
        digest.direct_messages.append(
            DigestDirectMessage(
                chat=row.chat_title,
                summary="Личное сообщение.",
                needs_reply=False,
                source_refs=[MessageRef(chat_id=row.chat_id, message_id=row.message_id)],
                needs_manual_review=False,
                message_count=1,
                first_message_at=row.timestamp,
                last_message_at=row.timestamp,
            )
        )
    return digest


def _row_lookup(rows: list) -> dict[tuple[str, int], object]:
    return {(row.chat_id, row.message_id): row for row in rows}


def _refs(item) -> list[tuple[str, int]]:
    return [(ref.chat_id, ref.message_id) for ref in item.source_refs]


def _metrics_for_refs(item, rows_by_ref: dict[tuple[str, int], object]) -> None:
    rows = [rows_by_ref[ref] for ref in _refs(item) if ref in rows_by_ref]
    if not rows:
        return
    item.message_count = len(rows)
    item.first_message_at = min(row.timestamp for row in rows)
    item.last_message_at = max(row.timestamp for row in rows)
    item.summary = _clean_summary(item.summary)


def _merge_items_by_chat(items: list) -> list:
    merged = {}
    for item in items:
        existing = merged.get(item.chat)
        item.summary = _clean_summary(item.summary)
        if not existing:
            merged[item.chat] = item
            continue
        existing_refs = {(ref.chat_id, ref.message_id) for ref in existing.source_refs}
        for ref in item.source_refs:
            if (ref.chat_id, ref.message_id) not in existing_refs:
                existing.source_refs.append(ref)
        if item.summary and item.summary not in existing.summary:
            existing.summary = _clean_summary(f"{existing.summary}; {item.summary}")
        existing.needs_reply = bool(
            getattr(existing, "needs_reply", False) or getattr(item, "needs_reply", False)
        )
        if not getattr(existing, "action", None) and getattr(item, "action", None):
            existing.action = item.action
    return list(merged.values())


def _enrich_digest(digest: DailyDigest, rows: list, overflow_notes: list[dict]) -> DailyDigest:
    rows_by_ref = _row_lookup(rows)
    digest.direct_messages = _merge_items_by_chat(digest.direct_messages)
    digest.group_updates = _merge_items_by_chat(digest.group_updates)
    for item in [*digest.direct_messages, *digest.group_updates, *digest.p0_alerts]:
        _metrics_for_refs(item, rows_by_ref)
    for note in overflow_notes:
        digest.review.append(
            DigestReviewItem(
                chat=note["chat"],
                reason="Лимит обработки",
                summary=note["summary"],
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


def generate_digest(
    session: Session,
    llm: HaikuClient,
    day: date,
    timezone: str,
    *,
    rows: list | None = None,
    max_messages_per_window: int = MAX_MESSAGES_PER_DIGEST_WINDOW,
    max_messages_per_chat: int = MAX_MESSAGES_PER_CHAT,
    max_chars_per_group: int = MAX_CHARS_PER_GROUP,
) -> DailyDigest:
    start, end = day_bounds(day, timezone)
    overflow_notes: list[dict] = []
    if rows is None:
        fetched = repository.messages_between(
            session,
            start,
            end,
            limit=max_messages_per_window + 1,
        )
        if len(fetched) > max_messages_per_window:
            overflow_notes.append(
                {
                    "chat": "Digest",
                    "summary": (
                        "Достигнут лимит digest window; часть сообщений будет обработана "
                        "в следующем запуске."
                    ),
                }
            )
            rows = fetched[:max_messages_per_window]
        else:
            rows = fetched
    payload = build_structured_payload(
        rows,
        max_messages_per_chat=max_messages_per_chat,
        max_chars_per_group=max_chars_per_group,
    )
    overflow_notes.extend(payload.pop("overflow_notes", []))
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
    digest = _protect_private_messages(digest, rows)
    return _enrich_digest(digest, rows, overflow_notes)


def _subject_for(digest: DailyDigest) -> str:
    if digest.generated_by == "fallback":
        return f"[FALLBACK] Telegram digest — {digest.date}"
    return digest_subject(digest)


def _deliver_pending_digest(
    session: Session,
    record,
    email_sender: EmailSender,
    now: datetime,
) -> bool:
    token = f"digest-pipeline-{record.id}-{int(now.timestamp())}"
    claimed = repository.claim_pending_digest(session, record.id, now, token)
    if not claimed:
        return False
    return repository.send_claimed_digest(session, claimed.id, token, email_sender, now)


def _deliver_after_payload_update(
    session: Session,
    record_id: int,
    digest: DailyDigest,
    email_sender: EmailSender,
    now: datetime,
    *,
    payload_updated: bool,
) -> DailyDigest:
    """Reload state after a CAS payload update, then use the sole claimed send path."""
    record = repository.get_digest_record(session, record_id)
    if not record:
        digest.email_status = "pending"
        return digest
    if not payload_updated and record.email_status in {"building", "sending", "sent"}:
        digest.email_status = record.email_status
        digest.error_summary = record.last_error_safe
        return digest
    if record.email_status == "pending" and _deliver_pending_digest(
        session, record, email_sender, now
    ):
        digest.email_status = "sent"
        return digest
    refreshed = repository.get_digest_record(session, record_id)
    if refreshed:
        digest.email_status = refreshed.email_status
        digest.error_summary = refreshed.last_error_safe
    return digest


def send_daily_digest_pipeline(
    session: Session,
    llm: HaikuClient,
    email_sender: EmailSender,
    day: date,
    timezone: str,
) -> DailyDigest:
    pending = repository.pending_digest_for_date(session, day.isoformat())
    if pending:
        if pending.email_status == "building":
            rows_for_digest = repository.messages_claimed_by_digest(session, pending.id)
            if not rows_for_digest:
                return DailyDigest(date=day.isoformat(), email_status="pending")
            record = pending
            rows = rows_for_digest
            digest = generate_digest(session, llm, day, timezone, rows=rows_for_digest)
            html = render_html(digest)
            text = render_plain_text(digest)
            subject = _subject_for(digest)
            digest.email_status = "pending"
            payload_updated = repository.update_digest_payload(
                session,
                record,
                digest,
                subject=subject,
                text=text,
                html=html,
            )
            now = datetime.now().astimezone()
            return _deliver_after_payload_update(
                session,
                record.id,
                digest,
                email_sender,
                now,
                payload_updated=payload_updated,
            )
        digest = repository.digest_from_record(pending)
        digest.email_status = pending.email_status
        return digest
    start, end = day_bounds(day, timezone)
    rows = repository.messages_between(
        session,
        start,
        end,
        limit=MAX_MESSAGES_PER_DIGEST_WINDOW + 1,
    )
    rows_for_digest = rows[:MAX_MESSAGES_PER_DIGEST_WINDOW]
    if not rows_for_digest:
        return DailyDigest(date=day.isoformat(), email_status="sent")
    record, claimed_rows, created = repository.claim_digest_run_for_rows(
        session,
        digest_date=day.isoformat(),
        rows=rows_for_digest,
    )
    if record is None:
        return DailyDigest(date=day.isoformat(), email_status="sent")
    if not created:
        digest = repository.digest_from_record(record)
        digest.email_status = record.email_status
        return digest
    rows_for_digest = claimed_rows
    digest = generate_digest(session, llm, day, timezone, rows=rows_for_digest)
    if len(rows) > MAX_MESSAGES_PER_DIGEST_WINDOW:
        digest.review.append(
            DigestReviewItem(
                chat="Digest",
                reason="Лимит обработки",
                summary=(
                    "Достигнут лимит digest window; часть сообщений будет обработана "
                    "в следующем запуске."
                ),
            )
        )
    html = render_html(digest)
    text = render_plain_text(digest)
    subject = _subject_for(digest)
    digest.email_status = "pending"
    payload_updated = repository.update_digest_payload(
        session,
        record,
        digest,
        subject=subject,
        text=text,
        html=html,
    )
    now = datetime.now().astimezone()
    return _deliver_after_payload_update(
        session,
        record.id,
        digest,
        email_sender,
        now,
        payload_updated=payload_updated,
    )
