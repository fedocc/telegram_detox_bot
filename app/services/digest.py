from __future__ import annotations

import logging
import re
from collections import Counter, defaultdict
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.db import repository
from app.email.render import digest_subject, render_html, render_plain_text
from app.email.sender import EmailSender
from app.llm.client import (
    HaikuClient,
    LLMError,
    sanitize_validation_codes,
    sanitize_validation_error_type,
    sanitize_validation_paths,
)
from app.models.schemas import (
    ChatType,
    DailyDigest,
    DigestDirectMessage,
    DigestGroupUpdate,
    DigestNoiseCount,
    DigestReviewItem,
    MessageRef,
)

logger = logging.getLogger(__name__)

APPROX_TOKEN_CHARS = 4
MAX_INPUT_TOKENS = 150_000
REVIEW_TEXT_LIMIT = 500
MAX_MESSAGES_PER_DIGEST_WINDOW = 5_000
MAX_MESSAGES_PER_CHAT = 100
MAX_CHARS_PER_GROUP = 12_000
BAD_SUMMARY_PHRASES = ("короткая переписка",)
USEFUL_FALLBACK_SUMMARY = "Была обычная переписка без явного запроса."
COUNT_ONLY_SUMMARY_RE = re.compile(
    r"^(?:"
    r"(?:всего\s+)?\d+\s+(?:новых\s+)?(?:сообщени(?:е|я|й)|messages?)|"
    r"(?:сообщени(?:е|я|й)|messages?)\s*:\s*\d+"
    r")[.!]?$",
    re.IGNORECASE,
)
FALLBACK_REQUEST_RE = re.compile(
    r"(?<!\w)(?:ответь|отпиши|позвони|набери|посмотри|проверь|пришли|"
    r"отправь|подтверди|можешь|нужен\s+ответ|надо\s+(?:обсудить|решить)|"
    r"reply|call|send|check|confirm)(?!\w)",
    re.IGNORECASE,
)
FALLBACK_DEADLINE_RE = re.compile(
    r"(?<!\w)(?:сейчас|сегодня|завтра|now|today|tomorrow|deadline|дедлайн|"
    r"(?:до|к)\s+\d{1,2}(?::\d{2})?|до\s+завтра|by\s+tomorrow)(?!\w)",
    re.IGNORECASE,
)
FALLBACK_EXACT_DEADLINE_RE = re.compile(
    r"(?<!\w)(?:до\s+завтра|к\s+завтра|"
    r"(?:сегодня|завтра)(?:\s+в)?\s+\d{1,2}(?::\d{2})?|"
    r"(?:до|к)\s+\d{1,2}(?::\d{2})?|сейчас|сегодня|завтра|"
    r"now|today|tomorrow)(?!\w)",
    re.IGNORECASE,
)
FALLBACK_URGENCY_RE = re.compile(
    r"(?<!\w)(?:срочно|важно|urgent|asap)(?!\w)",
    re.IGNORECASE,
)
FALLBACK_ANSWER_RE = re.compile(
    r"(?<!\w)(?:ответь|отпиши|дай\s+знать|reply)(?!\w)",
    re.IGNORECASE,
)
FALLBACK_READINESS_RE = re.compile(
    r"(?<!\w)(?:ты\s+)?готов(?:а|ы)?\s*\?",
    re.IGNORECASE,
)
FALLBACK_ACTION_PATTERNS = (
    (re.compile(r"(?<!\w)(?:позвони|набери|call)(?!\w)", re.IGNORECASE), "позвонить"),
    (re.compile(r"(?<!\w)(?:пришли|отправь|send)(?!\w)", re.IGNORECASE), "отправить"),
    (re.compile(r"(?<!\w)(?:посмотри|проверь|check)(?!\w)", re.IGNORECASE), "проверить"),
    (re.compile(r"(?<!\w)(?:подтверди|confirm)(?!\w)", re.IGNORECASE), "подтвердить"),
)
MEDIA_LABELS = {
    "photo": "фото",
    "voice": "голосовое",
    "video": "видео",
    "document": "документ",
    "other": "медиафайл",
}


def safe_truncate(text: str | None, limit: int = REVIEW_TEXT_LIMIT) -> str:
    if not text:
        return ""
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 3] + "..."


def _clean_summary(text: str | None) -> str:
    summary = safe_truncate(text, 280).strip()
    if not summary or COUNT_ONLY_SUMMARY_RE.fullmatch(summary):
        return USEFUL_FALLBACK_SUMMARY
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
                "chat_type": (
                    "group"
                    if _chat_type_value(row) == ChatType.supergroup.value
                    else _chat_type_value(row)
                ),
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
    merged.diagnostics.llm_attempted = any(
        digest.diagnostics.llm_attempted for digest in digests
    )
    merged.diagnostics.llm_used = any(digest.diagnostics.llm_used for digest in digests)
    merged.diagnostics.fallback_used = any(
        digest.diagnostics.fallback_used for digest in digests
    )
    reasons = sorted(
        {
            digest.diagnostics.fallback_reason
            for digest in digests
            if digest.diagnostics.fallback_reason
        }
    )
    merged.diagnostics.fallback_reason = ",".join(reasons) or None
    validation_types = sorted(
        {
            digest.diagnostics.validation_error_type
            for digest in digests
            if digest.diagnostics.validation_error_type
        }
    )
    merged.diagnostics.validation_error_type = ",".join(validation_types) or None
    merged.diagnostics.validation_error_paths = list(
        dict.fromkeys(
            path
            for digest in digests
            for path in digest.diagnostics.validation_error_paths
        )
    )
    merged.diagnostics.validation_error_codes = list(
        dict.fromkeys(
            code
            for digest in digests
            for code in digest.diagnostics.validation_error_codes
        )
    )
    merged.diagnostics.repair_attempted = any(
        digest.diagnostics.repair_attempted for digest in digests
    )
    merged.diagnostics.repair_used = any(
        digest.diagnostics.repair_used for digest in digests
    )
    merged.diagnostics.expected_chat_count = sum(
        digest.diagnostics.expected_chat_count for digest in digests
    )
    merged.diagnostics.returned_chat_count = sum(
        digest.diagnostics.returned_chat_count for digest in digests
    )
    merged.diagnostics.missing_chat_count = sum(
        digest.diagnostics.missing_chat_count for digest in digests
    )
    merged.diagnostics.duplicate_chat_count = sum(
        digest.diagnostics.duplicate_chat_count for digest in digests
    )
    merged.diagnostics.unknown_chat_count = sum(
        digest.diagnostics.unknown_chat_count for digest in digests
    )
    if merged.diagnostics.fallback_used:
        merged.generated_by = "fallback"
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


def _attach_collapsed_media_summaries(digest: DailyDigest, rows: list) -> None:
    grouped: dict[tuple[str, str], list] = defaultdict(list)
    for row in rows:
        if not row.is_outgoing and row.media_type != "none":
            grouped[(row.chat_id, row.chat_title)].append(row)
    for (chat_id, chat_title), media_rows in grouped.items():
        counts = Counter(str(row.media_type) for row in media_rows)
        detail = ", ".join(
            f"{count} {MEDIA_LABELS.get(media_type, 'медиафайл')}"
            for media_type, count in sorted(counts.items())
        )
        media_summary = f"Медиа: {detail} — содержимое не анализировалось."
        items = [
            item
            for item in [*digest.direct_messages, *digest.group_updates]
            if any(ref.chat_id == chat_id for ref in item.source_refs)
        ]
        if items:
            for item in items:
                item.media_summary = media_summary
            continue
        chat_type = _chat_type_value(media_rows[0])
        refs = [MessageRef(chat_id=chat_id, message_id=row.message_id) for row in media_rows]
        if chat_type == ChatType.private.value:
            item = DigestDirectMessage(
                chat=chat_title,
                summary="Получены сообщения с медиа.",
                needs_reply=False,
                source_refs=refs,
            )
            item.media_summary = media_summary
            digest.direct_messages.append(item)
        else:
            item = DigestGroupUpdate(
                chat=chat_title,
                summary="Получены сообщения с медиа.",
                source_refs=refs,
            )
            item.media_summary = media_summary
            digest.group_updates.append(item)


def fallback_digest(day: date, rows: list) -> DailyDigest:
    direct: list[DigestDirectMessage] = []
    group_updates: list[DigestGroupUpdate] = []
    review: list[DigestReviewItem] = []
    noise_counts: list[DigestNoiseCount] = []
    grouped: dict[tuple[str, str], list] = defaultdict(list)
    for row in rows:
        grouped[(row.chat_id, row.chat_title)].append(row)
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
    for (_, chat), chat_rows in grouped.items():
        incoming = [r for r in chat_rows if not r.is_outgoing]
        if incoming and _chat_type_value(incoming[0]) == ChatType.private.value:
            first = min(r.timestamp for r in incoming)
            last = max(r.timestamp for r in incoming)
            fallback_semantics = _fallback_semantics(incoming)
            direct.append(
                DigestDirectMessage(
                    chat=chat,
                    summary=str(fallback_semantics["summary"]),
                    needs_reply=fallback_semantics["possible_request"],
                    action=None,
                    what_happened=str(fallback_semantics["summary"]),
                    requests_to_me=fallback_semantics["requests_to_me"],
                    important_context=fallback_semantics["important_context"],
                    action_items=fallback_semantics["action_items"],
                    should_open_telegram=True,
                    open_reason=fallback_semantics["open_reason"],
                    open_telegram=True,
                    deadline_text=fallback_semantics["deadline_text"],
                    source_refs=[
                        MessageRef(chat_id=r.chat_id, message_id=r.message_id)
                        for r in incoming
                    ],
                    needs_manual_review=False,
                    message_count=len(incoming),
                    first_message_at=first,
                    last_message_at=last,
                )
            )
        else:
            first = min(r.timestamp for r in chat_rows)
            last = max(r.timestamp for r in chat_rows)
            direct_refs = [
                MessageRef(chat_id=r.chat_id, message_id=r.message_id)
                for r in chat_rows
            ]
            if chat_rows and _chat_type_value(chat_rows[0]) in {
                ChatType.group.value,
                ChatType.supergroup.value,
                ChatType.channel.value,
            }:
                fallback_semantics = _fallback_semantics(chat_rows)
                # Use group updates for fallback digests so user sees one concise group line.
                group_updates.append(
                    DigestGroupUpdate(
                        chat=chat,
                        summary=str(fallback_semantics["summary"]),
                        what_happened=str(fallback_semantics["summary"]),
                        requests_to_me=fallback_semantics["requests_to_me"],
                        important_context=fallback_semantics["important_context"],
                        action_items=fallback_semantics["action_items"],
                        should_open_telegram=True,
                        open_reason=fallback_semantics["open_reason"],
                        deadline_text=fallback_semantics["deadline_text"],
                        source_refs=direct_refs,
                        message_count=len(chat_rows),
                        first_message_at=first,
                        last_message_at=last,
                    )
                )
            else:
                noise_counts.append(DigestNoiseCount(chat=chat, count=len(chat_rows)))
    digest = DailyDigest(
        date=day.isoformat(),
        direct_messages=direct,
        group_updates=group_updates,
        review=review,
        noise_counts=noise_counts,
        generated_by="fallback",
    )
    digest.diagnostics.fallback_used = True
    digest.diagnostics.fallback_reason = "deterministic_fallback"
    _attach_collapsed_media_summaries(digest, rows)
    return digest


def _local_chat_summary(chat_rows: list) -> str:
    snippets = [
        safe_truncate(row.text or row.caption, 120)
        for row in chat_rows[-3:]
        if row.text or row.caption
    ]
    if snippets:
        return _clean_summary("; ".join(snippets))
    return "Получены сообщения с медиа без подписи."


def _append_unique(values: list[str], value: str) -> None:
    if value not in values:
        values.append(value)


def _join_russian(values: list[str]) -> str:
    if not values:
        return ""
    if len(values) == 1:
        return values[0]
    return ", ".join(values[:-1]) + " и " + values[-1]


def _fallback_semantics(chat_rows: list) -> dict[str, str | bool | None]:
    raw_text = "\n".join(
        row.text or row.caption or "" for row in chat_rows if row.text or row.caption
    )
    requests: list[str] = []
    if FALLBACK_ANSWER_RE.search(raw_text):
        _append_unique(requests, "ответить")
    readiness = bool(FALLBACK_READINESS_RE.search(raw_text))
    if readiness:
        _append_unique(requests, "подтвердить готовность")
    for pattern, action_name in FALLBACK_ACTION_PATTERNS:
        if pattern.search(raw_text):
            _append_unique(requests, action_name)
    if "?" in raw_text and not requests:
        _append_unique(requests, "ответить на вопрос")

    deadlines = [
        match.group(0).strip()
        for match in FALLBACK_EXACT_DEADLINE_RE.finditer(raw_text)
    ]
    deadline_values: list[str] = []
    for deadline in deadlines:
        _append_unique(deadline_values, deadline)
    deadline_text = "; ".join(deadline_values) or None
    possible_deadline = bool(deadline_text or FALLBACK_DEADLINE_RE.search(raw_text))
    urgent = bool(FALLBACK_URGENCY_RE.search(raw_text))
    possible_request = bool(requests or FALLBACK_REQUEST_RE.search(raw_text))

    context_parts: list[str] = []
    if re.search(r"(?<!\w)(?:вылет|рейс)(?!\w)", raw_text, re.IGNORECASE):
        context_parts.append(f"{deadline_text} вылет" if deadline_text else "предстоящий вылет")
    elif deadline_text:
        context_parts.append(f"указан срок: {deadline_text}")
    if urgent:
        context_parts.append("сообщение помечено как срочное")

    if FALLBACK_ANSWER_RE.search(raw_text) and readiness:
        flight_context = " к вылету" if "вылет" in raw_text.lower() else ""
        deadline_context = f" {deadline_text}" if deadline_text else ""
        summary = (
            "Были сообщения с просьбой ответить и вопросом о готовности"
            f"{flight_context}{deadline_context}."
        )
    elif requests:
        summary = f"Был запрос: {_join_russian(requests)}."
    elif urgent:
        summary = "Было срочное сообщение."
    elif deadline_text:
        summary = f"В переписке указан срок: {deadline_text}."
    else:
        summary = _local_chat_summary(chat_rows)

    if requests:
        request_text = "; ".join(requests)
    else:
        request_text = "Явный запрос не найден локальными правилами."
    if requests:
        action = "Открыть Telegram и ответить."
    elif urgent or possible_deadline:
        action = "Открыть Telegram и проверить сообщение."
    else:
        action = "Просмотреть краткое резюме; явного действия локально не найдено."
    important_context = "; ".join(context_parts) or _local_chat_summary(chat_rows)
    return {
        "summary": _clean_summary(summary),
        "possible_request": possible_request,
        "requests_to_me": request_text,
        "important_context": important_context,
        "action_items": action,
        "open_reason": (
            "Есть запрос, вопрос, срочность или срок."
            if possible_request or urgent or possible_deadline
            else "Сводка построена без анализа LLM."
        ),
        "deadline_text": deadline_text,
    }


def _item_for_chat(items: list, chat_id: str):
    for item in items:
        if any(ref.chat_id == chat_id for ref in item.source_refs):
            return item
    return None


def _drop_cross_chat_items(digest: DailyDigest) -> DailyDigest:
    """Reject LLM summaries that blur multiple conversations into one item."""

    def belongs_to_one_chat(item) -> bool:
        return len({ref.chat_id for ref in item.source_refs}) == 1

    digest.direct_messages = [
        item for item in digest.direct_messages if belongs_to_one_chat(item)
    ]
    digest.group_updates = [
        item for item in digest.group_updates if belongs_to_one_chat(item)
    ]
    return digest


def _ensure_chat_summaries(digest: DailyDigest, rows: list) -> DailyDigest:
    grouped: dict[str, list] = defaultdict(list)
    for row in rows:
        if not row.is_outgoing:
            grouped[row.chat_id].append(row)

    summarized_titles: set[str] = set()
    for chat_id, chat_rows in grouped.items():
        first_row = chat_rows[0]
        summarized_titles.add(first_row.chat_title)
        is_private = _chat_type_value(first_row) == ChatType.private.value
        items = digest.direct_messages if is_private else digest.group_updates
        item = _item_for_chat(items, chat_id)
        refs = [MessageRef(chat_id=chat_id, message_id=row.message_id) for row in chat_rows]
        if item is not None:
            existing_refs = {(ref.chat_id, ref.message_id) for ref in item.source_refs}
            item.source_refs.extend(
                ref
                for ref in refs
                if (ref.chat_id, ref.message_id) not in existing_refs
            )
            continue

        summary = _local_chat_summary(chat_rows)
        fallback_semantics = _fallback_semantics(chat_rows)
        semantic = {
            "chat": first_row.chat_title,
            "summary": summary,
            "what_happened": summary,
            "requests_to_me": fallback_semantics["requests_to_me"],
            "important_context": fallback_semantics["important_context"],
            "action_items": fallback_semantics["action_items"],
            "should_open_telegram": True,
            "open_reason": fallback_semantics["open_reason"],
            "source_refs": refs,
            "needs_manual_review": False,
        }
        if is_private:
            digest.direct_messages.append(
                DigestDirectMessage(
                    needs_reply=bool(fallback_semantics["possible_request"]),
                    **semantic,
                )
            )
        else:
            digest.group_updates.append(DigestGroupUpdate(**semantic))

    digest.noise_counts = [
        count for count in digest.noise_counts if count.chat not in summarized_titles
    ]
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
    if item.what_happened:
        item.what_happened = _clean_summary(item.what_happened)


def _merge_items_by_chat(items: list) -> list:
    merged = {}
    for item in items:
        chat_ids = {ref.chat_id for ref in item.source_refs}
        key = next(iter(chat_ids)) if len(chat_ids) == 1 else item.chat
        existing = merged.get(key)
        item.summary = _clean_summary(item.summary)
        if not existing:
            merged[key] = item
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
        for attribute in (
            "what_happened",
            "requests_to_me",
            "important_context",
            "action_items",
            "open_reason",
            "requests",
            "context",
            "open_telegram_reason",
        ):
            if not getattr(existing, attribute, None) and getattr(item, attribute, None):
                setattr(existing, attribute, getattr(item, attribute))
        if getattr(existing, "should_open_telegram", None) is None:
            existing.should_open_telegram = getattr(item, "should_open_telegram", None)
        existing.open_telegram = bool(
            getattr(existing, "open_telegram", False) or getattr(item, "open_telegram", False)
        )
        if not getattr(existing, "media_summary", None) and getattr(item, "media_summary", None):
            existing.media_summary = item.media_summary
    return list(merged.values())


def _ensure_semantic_completeness(digest: DailyDigest) -> None:
    for item in [*digest.direct_messages, *digest.group_updates]:
        item.summary = _clean_summary(item.summary)
        item.what_happened = _clean_summary(item.what_happened or item.summary)
        item.requests_to_me = item.requests_to_me or item.requests or "Не определено."
        item.important_context = item.important_context or item.context or "Не определено."
        item.action_items = item.action_items or item.action or "Не определено; проверьте чат."
        if item.should_open_telegram is None:
            item.should_open_telegram = True
        item.open_reason = (
            item.open_reason
            or item.open_telegram_reason
            or "Нужна проверка контекста переписки."
        )


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
    _ensure_semantic_completeness(digest)
    return digest


def _call_daily_digest(llm: HaikuClient, payload: dict, day: date, rows: list) -> DailyDigest:
    try:
        digest = llm.daily_digest(payload)
        digest.diagnostics.llm_attempted = True
        digest.diagnostics.llm_used = True
        return digest
    except (LLMError, TimeoutError, RuntimeError, ValueError) as exc:
        digest = fallback_digest(day, rows)
        digest.diagnostics.llm_attempted = True
        digest.diagnostics.llm_used = False
        digest.diagnostics.fallback_used = True
        if isinstance(exc, LLMError):
            digest.diagnostics.fallback_reason = exc.reason_code
            digest.diagnostics.validation_error_type = exc.validation_error_type
            digest.diagnostics.validation_error_paths = exc.validation_error_paths
            digest.diagnostics.validation_error_codes = exc.validation_error_codes
            digest.diagnostics.repair_attempted = exc.repair_attempted
            digest.diagnostics.repair_used = exc.repair_used
            digest.diagnostics.expected_chat_count = exc.expected_chat_count
            digest.diagnostics.returned_chat_count = exc.returned_chat_count
            digest.diagnostics.missing_chat_count = exc.missing_chat_count
            digest.diagnostics.duplicate_chat_count = exc.duplicate_chat_count
            digest.diagnostics.unknown_chat_count = exc.unknown_chat_count
        else:
            digest.diagnostics.fallback_reason = "llm_runtime_error"
        digest.error_summary = digest.diagnostics.fallback_reason
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
    ignored_chat_ids: frozenset[str] | set[str] | None = None,
) -> DailyDigest:
    start, end = day_bounds(day, timezone)
    overflow_notes: list[dict] = []
    if rows is None:
        fetched = repository.messages_between(
            session,
            start,
            end,
            limit=max_messages_per_window + 1,
            excluded_chat_ids=ignored_chat_ids,
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
    elif ignored_chat_ids:
        rows = [row for row in rows if row.chat_id not in ignored_chat_ids]
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
                chunk_digest = fallback_digest(day, chunk_rows)
                chunk_digest.diagnostics.fallback_reason = "input_too_large"
                chunk_digests.append(chunk_digest)
            else:
                chat_ids = {chat["chat_id"] for chat in chunk}
                chunk_rows = [row for row in rows if row.chat_id in chat_ids]
                chunk_digests.append(_call_daily_digest(llm, chunk_payload, day, chunk_rows))
        digest = _merge_digests(day, chunk_digests)
    digest = _drop_cross_chat_items(digest)
    digest = _ensure_chat_summaries(digest, rows)
    _attach_collapsed_media_summaries(digest, rows)
    digest = _enrich_digest(digest, rows, overflow_notes)
    digest.diagnostics.chats_count = len({row.chat_id for row in rows})
    digest.diagnostics.messages_count = len(rows)
    if digest.diagnostics.expected_chat_count == 0:
        digest.diagnostics.expected_chat_count = digest.diagnostics.chats_count
    if (
        not digest.diagnostics.llm_used
        and digest.diagnostics.returned_chat_count == 0
        and digest.diagnostics.missing_chat_count == 0
    ):
        digest.diagnostics.missing_chat_count = digest.diagnostics.expected_chat_count
    safe_validation_type = sanitize_validation_error_type(
        digest.diagnostics.validation_error_type
    )
    safe_validation_paths = sanitize_validation_paths(
        digest.diagnostics.validation_error_paths
    )
    safe_validation_codes = sanitize_validation_codes(
        digest.diagnostics.validation_error_codes
    )
    digest.diagnostics.validation_error_type = safe_validation_type
    digest.diagnostics.validation_error_paths = safe_validation_paths
    digest.diagnostics.validation_error_codes = safe_validation_codes
    logger.info(
        "Digest generation diagnostics llm_attempted=%s llm_used=%s fallback_used=%s "
        "fallback_reason=%s chats_count=%d messages_count=%d validation_error_type=%s "
        "validation_error_paths=%s validation_error_codes=%s repair_attempted=%s "
        "repair_used=%s expected_chat_count=%d returned_chat_count=%d "
        "missing_chat_count=%d duplicate_chat_count=%d unknown_chat_count=%d",
        digest.diagnostics.llm_attempted,
        digest.diagnostics.llm_used,
        digest.diagnostics.fallback_used,
        digest.diagnostics.fallback_reason or "none",
        digest.diagnostics.chats_count,
        digest.diagnostics.messages_count,
        safe_validation_type or "none",
        ",".join(safe_validation_paths) or "none",
        ",".join(safe_validation_codes) or "none",
        digest.diagnostics.repair_attempted,
        digest.diagnostics.repair_used,
        digest.diagnostics.expected_chat_count,
        digest.diagnostics.returned_chat_count,
        digest.diagnostics.missing_chat_count,
        digest.diagnostics.duplicate_chat_count,
        digest.diagnostics.unknown_chat_count,
    )
    return digest


def _subject_for(digest: DailyDigest) -> str:
    if digest.generated_by == "fallback":
        return f"[Telegram Detox][Digest] [FALLBACK] Telegram digest — {digest.date}"
    return f"[Telegram Detox][Digest] {digest_subject(digest)}"


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
    *,
    ignored_chat_ids: frozenset[str] | set[str] | None = None,
) -> DailyDigest:
    pending = repository.pending_digest_for_date(session, day.isoformat())
    if pending:
        if pending.email_status == "building":
            rows_for_digest = repository.messages_claimed_by_digest(
                session,
                pending.id,
                excluded_chat_ids=ignored_chat_ids,
            )
            if not rows_for_digest:
                return DailyDigest(date=day.isoformat(), email_status="pending")
            record = pending
            rows = rows_for_digest
            digest = generate_digest(
                session,
                llm,
                day,
                timezone,
                rows=rows_for_digest,
                ignored_chat_ids=ignored_chat_ids,
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
        digest = repository.digest_from_record(pending)
        digest.email_status = pending.email_status
        return digest
    start, end = day_bounds(day, timezone)
    rows = repository.messages_between(
        session,
        start,
        end,
        limit=MAX_MESSAGES_PER_DIGEST_WINDOW + 1,
        excluded_chat_ids=ignored_chat_ids,
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
    digest = generate_digest(
        session,
        llm,
        day,
        timezone,
        rows=rows_for_digest,
        ignored_chat_ids=ignored_chat_ids,
    )
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
