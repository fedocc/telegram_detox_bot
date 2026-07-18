from __future__ import annotations

import re
from datetime import timedelta

from sqlalchemy.orm import Session

from app.config import Settings
from app.db import repository
from app.email.sender import EmailSender
from app.llm.client import HaikuClient, LLMError
from app.models.schemas import (
    P0_MIN_CONFIDENCE,
    ChatType,
    MediaType,
    P0Decision,
    P0Status,
    StoredMessage,
)
from app.services.digest import safe_truncate

SAFE_TEXT_LIMIT = 500
DEFAULT_MAX_CONTEXT_MESSAGES = 5
DEFAULT_MAX_MESSAGE_CHARS = 1000
DEFAULT_MAX_LLM_CALLS_PER_HOUR = 100

REQUEST_OR_ACTION_RE = re.compile(
    r"(?<!\w)(?:"
    r"ответь|позвони|набери|посмотри|проверь|пришли|отправь|подтверди|"
    r"можешь\s+(?:(?:сегодня|сейчас|потом)\s+)?"
    r"(?:ответить|позвонить|посмотреть|проверить|прислать|отправить|подтвердить)|"
    r"нужен\s+(?:ответ|код|файл)|"
    r"call\s+me|reply\s+please|"
    r"can\s+we\s+call|"
    r"(?:can|could)\s+you\s+(?:reply|call|check|send|confirm)|"
    r"send\s+me|check\s+this"
    r")(?!\w)",
    re.IGNORECASE,
)
URGENCY_RE = re.compile(r"\b(?:asap|important|urgent|важн\w*|сроч\w*)\b", re.IGNORECASE)
EXPLICIT_DEADLINE_RE = re.compile(
    r"\b(?:at\s+\d{1,2}(?::\d{2})?|by\s+\d{1,2}(?::\d{2})?|deadline|"
    r"in\s+(?:\d+|one|two|three|thirty)\s+(?:minutes?|hours?)|"
    r"within\s+\d+\s+(?:minutes?|hours?)|до\s+\d{1,2}(?::\d{2})?|"
    r"сегодня\s+до\s+\d{1,2}(?::\d{2})?|"
    r"через\s+(?:\d+\s+)?(?:минут\w*|час\w*))\b",
    re.IGNORECASE,
)


def _message_payload(
    message: StoredMessage,
    context: list,
    max_message_chars: int,
    *,
    trusted_sender: bool,
    policy_context: dict[str, bool],
) -> dict:
    capped_text = safe_truncate(message.text or message.caption, max_message_chars)
    return {
        "message": {
            "chat_id": message.chat_id,
            "chat_title": message.chat_title,
            "chat_type": message.chat_type.value,
            "sender_name": message.sender_name,
            "message_id": message.message_id,
            "timestamp": message.timestamp.isoformat(),
            "text": capped_text,
            "media_type": message.media_type.value,
            "is_outgoing": message.is_outgoing,
            "trusted_sender": trusted_sender,
            "policy": policy_context,
        },
        "context": [
            {
                "sender": row.sender_name,
                "is_outgoing": row.is_outgoing,
                "text": safe_truncate(row.text or row.caption, SAFE_TEXT_LIMIT),
                "message_id": row.message_id,
            }
            for row in context
        ],
    }


def _context_with_reply_parent(session: Session, message: StoredMessage, limit: int) -> list:
    if limit <= 0:
        context = []
    else:
        context = repository.recent_chat_context(session, message.chat_id, limit=limit)
    if message.reply_to_message_id:
        parent = repository.get_message(session, message.chat_id, message.reply_to_message_id)
        if parent and all(row.message_id != parent.message_id for row in context):
            context.insert(0, parent)
    return context[-limit:] if limit > 0 else context[:1]


def _deadline_line(decision) -> str:
    deadline_at = getattr(decision, "deadline_at", None)
    if deadline_at:
        return deadline_at.isoformat()
    return getattr(decision, "deadline_text", None) or "-"


def _decision_body(message: StoredMessage, decision) -> str:
    raw_text = message.text or message.caption or ""
    reason = getattr(decision, "reason", None) or decision.summary
    parts = [
        f"Чат: {message.chat_title}",
        f"Отправитель: {message.sender_name or 'Unknown'}",
        f"Время: {message.timestamp.isoformat()}",
        f"Исходный текст:\n{raw_text}",
        f"Почему P0_STRICT: {reason}",
        f"Конкретное действие: {decision.action or '-'}",
        f"Дедлайн: {_deadline_line(decision)}",
    ]
    return "\n\n".join(parts)


def _has_text(message: StoredMessage) -> bool:
    return bool((message.text or message.caption or "").strip())


def _is_private(message: StoredMessage) -> bool:
    return message.chat_type == ChatType.private


def _is_groupish(message: StoredMessage) -> bool:
    return message.chat_type in {ChatType.group, ChatType.supergroup, ChatType.channel}


def _is_non_text_media(message: StoredMessage) -> bool:
    return message.media_type != MediaType.none and not _has_text(message)


def _mention_usernames(settings: Settings | None) -> set[str]:
    configured = settings.p0_mention_usernames if settings is not None else "me,fedornikonov"
    return {
        item.strip().removeprefix("@").casefold()
        for item in configured.split(",")
        if item.strip().removeprefix("@")
    }


def _mentions_me(message: StoredMessage, settings: Settings | None) -> bool:
    text = message.text or message.caption or ""
    mentioned_usernames = {
        match.casefold()
        for match in re.findall(r"(?<!\w)@([A-Za-z0-9_]+)(?![A-Za-z0-9_])", text)
    }
    return bool(mentioned_usernames & _mention_usernames(settings))


def _replies_to_me(session: Session, message: StoredMessage) -> bool:
    if not message.reply_to_message_id:
        return False
    parent = repository.get_message(session, message.chat_id, message.reply_to_message_id)
    if parent is None:
        # TODO: Persist reply-parent direction from Telegram when the parent is unavailable.
        return False
    return parent.is_outgoing


def _watchlist_contains(settings: Settings | None, chat_id: str) -> bool:
    if settings is None:
        return False
    watched = {
        item.strip()
        for item in settings.p0_watchlist_chat_ids.split(",")
        if item.strip()
    }
    return chat_id in watched


def _trusted_sender(settings: Settings | None, message: StoredMessage) -> bool:
    if settings is None or not message.sender_id:
        return False
    trusted = {
        item.strip()
        for item in settings.p0_trusted_sender_ids.split(",")
        if item.strip()
    }
    return message.sender_id in trusted


def _watchlist_keyword_matches(settings: Settings | None, raw_text: str) -> bool:
    if settings is None:
        return False
    normalized = raw_text.casefold()
    return any(
        keyword.strip().casefold() in normalized
        for keyword in settings.p0_watchlist_keywords.split(",")
        if keyword.strip()
    )


def _has_request_or_action(raw_text: str) -> bool:
    return bool(REQUEST_OR_ACTION_RE.search(raw_text))


def _has_urgency(raw_text: str) -> bool:
    return bool(URGENCY_RE.search(raw_text))


def _has_private_signal(raw_text: str) -> bool:
    return bool(_has_request_or_action(raw_text) or _has_urgency(raw_text))


def _group_policy_context(
    session: Session,
    message: StoredMessage,
    settings: Settings | None,
) -> dict[str, bool]:
    raw_text = message.text or message.caption or ""
    direct_mention = _mentions_me(message, settings)
    reply_to_me = _replies_to_me(session, message)
    request_or_urgency = _has_request_or_action(raw_text) or _has_urgency(raw_text)
    explicit_deadline = bool(EXPLICIT_DEADLINE_RE.search(raw_text))
    watchlist_match = bool(
        _watchlist_keyword_matches(settings, raw_text)
        or (
            settings is not None
            and settings.p0_classify_watchlist_chats
            and _watchlist_contains(settings, message.chat_id)
        )
    )
    mention_enabled = settings is None or settings.p0_classify_mentions
    reply_enabled = settings is None or settings.p0_classify_replies
    watchlist_enabled = settings is None or settings.p0_classify_watchlist_chats
    deterministic_strict = bool(
        (mention_enabled and direct_mention)
        or (reply_enabled and reply_to_me and request_or_urgency)
        or (explicit_deadline and request_or_urgency)
        or (watchlist_enabled and watchlist_match and request_or_urgency)
    )
    return {
        "direct_mention": direct_mention,
        "reply_to_me": reply_to_me,
        "request_or_urgency": request_or_urgency,
        "explicit_deadline": explicit_deadline,
        "watchlist_match": watchlist_match,
        "deterministic_strict": deterministic_strict,
    }


def _policy_context(
    session: Session,
    message: StoredMessage,
    settings: Settings | None,
) -> dict[str, bool]:
    raw_text = message.text or message.caption or ""
    if _is_private(message):
        request_or_urgency = bool(
            _has_request_or_action(raw_text) or _has_urgency(raw_text)
        )
        private_signal = _has_private_signal(raw_text)
        deterministic_strict = request_or_urgency
        return {
            "private_signal": private_signal,
            "direct_mention": False,
            "reply_to_me": False,
            "request_or_urgency": request_or_urgency,
            "explicit_deadline": bool(EXPLICIT_DEADLINE_RE.search(raw_text)),
            "watchlist_match": False,
            "deterministic_strict": deterministic_strict,
        }
    return {"private_signal": False, **_group_policy_context(session, message, settings)}


def _should_classify_immediately(
    session: Session,
    message: StoredMessage,
    settings: Settings | None,
    policy_context: dict[str, bool],
) -> bool:
    if message.is_outgoing or not _has_text(message):
        return False
    if _is_private(message):
        return True if settings is None else settings.p0_classify_private_text
    if not _is_groupish(message):
        return False
    if settings is not None and settings.p0_classify_all_groups:
        return True
    return policy_context["deterministic_strict"]


def _max_context_messages(settings: Settings | None) -> int:
    if settings is None:
        return DEFAULT_MAX_CONTEXT_MESSAGES
    return settings.p0_max_context_messages


def _max_message_chars(settings: Settings | None) -> int:
    if settings is None:
        return DEFAULT_MAX_MESSAGE_CHARS
    return settings.p0_max_message_chars


def _hourly_cap(settings: Settings | None) -> int:
    if settings is None:
        return DEFAULT_MAX_LLM_CALLS_PER_HOUR
    return settings.p0_max_llm_calls_per_hour


def _send_immediate_alert(
    session: Session,
    message: StoredMessage,
    email_sender: EmailSender,
    *,
    subject: str,
    body: str,
    html: str | None,
    alert_type: str,
) -> bool:
    job = repository.create_alert_job(
        session,
        chat_id=message.chat_id,
        message_id=message.message_id,
        alert_type=alert_type,
        subject=subject,
        text_body=body,
        html_body=html or "",
        now=message.timestamp,
    )
    if job.status != "pending" or job.attempts > 0:
        return True
    if repository.send_alert_job(session, job, email_sender, message.timestamp):
        repository.mark_alert_sent(session, message.chat_id, message.message_id)
    return True


def _mark_budget_review(
    session: Session,
    message: StoredMessage,
) -> bool:
    repository.mark_p0_review_candidate(session, message.chat_id, message.message_id)
    repository.mark_p0_classified(
        session,
        message.chat_id,
        message.message_id,
        P0Status.p0_candidate.value,
        message.timestamp,
    )
    return False


def _mark_candidate(
    session: Session,
    message: StoredMessage,
    confidence: float | None = None,
) -> bool:
    repository.mark_p0_review_candidate(session, message.chat_id, message.message_id)
    repository.mark_p0_classified(
        session,
        message.chat_id,
        message.message_id,
        P0Status.p0_candidate.value,
        message.timestamp,
        confidence=confidence,
    )
    return False


def _mark_not_p0(
    session: Session,
    message: StoredMessage,
    confidence: float | None = None,
) -> bool:
    repository.mark_p0_classified(
        session,
        message.chat_id,
        message.message_id,
        P0Status.not_p0.value,
        message.timestamp,
        confidence=confidence,
    )
    return False


def _local_strict_reason(
    message: StoredMessage,
    policy_context: dict[str, bool],
) -> str:
    if _is_private(message):
        return "Incoming private message contains a response/action request or urgent wording."
    if policy_context["direct_mention"]:
        return "Group message directly mentions the user."
    if policy_context["reply_to_me"]:
        return "Group message replies to the user and contains a request or urgency."
    if policy_context["explicit_deadline"]:
        return "Group message contains an explicit deadline plus a request or urgency."
    return "Group watchlist matched a message containing a request or urgency."


def _promote_to_strict(
    message: StoredMessage,
    policy_context: dict[str, bool],
    decision: P0Decision | None = None,
) -> P0Decision:
    reason = _local_strict_reason(message, policy_context)
    return P0Decision(
        status=P0Status.p0_strict,
        summary=decision.summary if decision else reason,
        reason=reason,
        action=(
            decision.action
            if decision and decision.action
            else "Open the original Telegram message and respond or act as requested."
        ),
        deadline_text=decision.deadline_text if decision else None,
        deadline_at=decision.deadline_at if decision else None,
        confidence=max(decision.confidence if decision else 1.0, P0_MIN_CONFIDENCE),
    )


def _decision_qualifies_for_strict(
    message: StoredMessage,
    policy_context: dict[str, bool],
    decision: P0Decision,
) -> bool:
    return bool(
        policy_context["deterministic_strict"]
        or (
            _is_private(message)
            and policy_context["private_signal"]
            and decision.status == P0Status.p0_strict
            and decision.confidence >= P0_MIN_CONFIDENCE
        )
    )


def _send_strict_decision(
    session: Session,
    message: StoredMessage,
    decision: P0Decision,
    email_sender: EmailSender,
) -> bool:
    repository.mark_p0_classified(
        session,
        message.chat_id,
        message.message_id,
        P0Status.p0_strict.value,
        message.timestamp,
        confidence=decision.confidence,
    )
    return _send_immediate_alert(
        session,
        message,
        email_sender,
        subject=f"[СРОЧНО] Telegram: {message.chat_title}",
        body=_decision_body(message, decision),
        html=None,
        alert_type="p0",
    )


def handle_p0_candidate(
    session: Session,
    message: StoredMessage,
    llm: HaikuClient,
    email_sender: EmailSender,
    settings: Settings | None = None,
) -> bool:
    if message.is_outgoing:
        return _mark_not_p0(session, message)
    existing = repository.get_message(session, message.chat_id, message.message_id)
    if existing and existing.alert_sent:
        return False
    if existing and existing.p0_classified_at:
        return False

    if _is_non_text_media(message):
        return _mark_not_p0(session, message)

    policy_context = _policy_context(session, message, settings)
    if not _should_classify_immediately(session, message, settings, policy_context):
        return False

    cap = _hourly_cap(settings)
    since = message.timestamp - timedelta(hours=1)
    if cap <= repository.p0_llm_calls_since(session, since):
        if policy_context["deterministic_strict"]:
            decision = _promote_to_strict(message, policy_context)
            return _send_strict_decision(session, message, decision, email_sender)
        return _mark_budget_review(session, message)

    context = _context_with_reply_parent(session, message, _max_context_messages(settings))
    try:
        repository.mark_p0_llm_called(
            session,
            message.chat_id,
            message.message_id,
            message.timestamp,
        )
        decision = llm.classify_p0(
            _message_payload(
                message,
                context,
                _max_message_chars(settings),
                trusted_sender=_trusted_sender(settings, message),
                policy_context=policy_context,
            )
        )
        if _decision_qualifies_for_strict(message, policy_context, decision):
            decision = _promote_to_strict(message, policy_context, decision)
            return _send_strict_decision(session, message, decision, email_sender)
        if decision.status == P0Status.not_p0:
            return _mark_not_p0(session, message, decision.confidence)
        return _mark_candidate(session, message, decision.confidence)
    except LLMError:
        if policy_context["deterministic_strict"]:
            decision = _promote_to_strict(message, policy_context)
            return _send_strict_decision(session, message, decision, email_sender)
        return _mark_candidate(session, message)
