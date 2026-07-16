from __future__ import annotations

from datetime import timedelta
from html import escape

from sqlalchemy.orm import Session

from app.config import Settings
from app.db import repository
from app.email.sender import EmailSender
from app.llm.client import HaikuClient, LLMError
from app.models.schemas import ChatType, MediaType, P0Status, StoredMessage
from app.services.digest import safe_truncate
from app.services.prefilter import is_p0_candidate, is_urgent_call_candidate

SAFE_TEXT_LIMIT = 500
DEFAULT_MAX_CONTEXT_MESSAGES = 5
DEFAULT_MAX_MESSAGE_CHARS = 1000
DEFAULT_MAX_LLM_CALLS_PER_HOUR = 100


def _message_payload(message: StoredMessage, context: list, max_message_chars: int) -> dict:
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


def _fallback_body(message: StoredMessage) -> tuple[str, str]:
    raw = safe_truncate(message.text or message.caption or "[media без текста]", SAFE_TEXT_LIMIT)
    text = (
        f"Chat: {message.chat_title}\n"
        f"Sender: {message.sender_name or 'Unknown'}\n"
        f"Timestamp: {message.timestamp.isoformat()}\n"
        f"Text: {raw}"
    )
    html = (
        f"<p><b>Chat:</b> {escape(message.chat_title)}</p>"
        f"<p><b>Sender:</b> {escape(message.sender_name or 'Unknown')}</p>"
        f"<p><b>Timestamp:</b> {escape(message.timestamp.isoformat())}</p>"
        f"<p><b>Text:</b> {escape(raw)}</p>"
    )
    return text, html


def _deadline_line(decision) -> str:
    deadline_at = getattr(decision, "deadline_at", None)
    if deadline_at:
        return deadline_at.isoformat()
    return getattr(decision, "deadline_text", None) or "-"


def _decision_body(message: StoredMessage, decision, reason: str | None = None) -> str:
    parts = [
        f"{message.sender_name or 'Unknown'}: {decision.summary}",
        f"Действие: {decision.action or '-'}",
        f"Срок: {_deadline_line(decision)}",
    ]
    if reason:
        parts.append(f"Reason: {reason}")
    return "\n\n".join(parts)


def _has_text(message: StoredMessage) -> bool:
    return bool((message.text or message.caption or "").strip())


def _is_private(message: StoredMessage) -> bool:
    return message.chat_type == ChatType.private


def _is_groupish(message: StoredMessage) -> bool:
    return message.chat_type in {ChatType.group, ChatType.supergroup, ChatType.channel}


def _is_non_text_media(message: StoredMessage) -> bool:
    return message.media_type != MediaType.none and not _has_text(message)


def _mentions_me(message: StoredMessage) -> bool:
    text = (message.text or message.caption or "").lower()
    return "@me" in text or "@fedornikonov" in text


def _replies_to_me(session: Session, message: StoredMessage) -> bool:
    if not message.reply_to_message_id:
        return False
    parent = repository.get_message(session, message.chat_id, message.reply_to_message_id)
    return bool(parent and parent.is_outgoing)


def _watchlist_contains(settings: Settings | None, chat_id: str) -> bool:
    if settings is None:
        return False
    watched = {
        item.strip()
        for item in settings.p0_watchlist_chat_ids.split(",")
        if item.strip()
    }
    return chat_id in watched


def _should_classify_immediately(
    session: Session,
    message: StoredMessage,
    settings: Settings | None,
    obvious: bool,
) -> bool:
    if message.is_outgoing or not _has_text(message):
        return False
    if _is_private(message):
        return True if settings is None else settings.p0_classify_private_text
    if not _is_groupish(message):
        return False
    if settings is not None and settings.p0_classify_all_groups:
        return True
    if settings is None:
        return obvious
    if settings.p0_classify_mentions and _mentions_me(message):
        return True
    if settings.p0_classify_replies and _replies_to_me(session, message):
        return True
    if settings.p0_classify_watchlist_chats and _watchlist_contains(settings, message.chat_id):
        return True
    return obvious


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


def _fail_open_budget_cap(
    session: Session,
    message: StoredMessage,
    email_sender: EmailSender,
) -> bool:
    text, html = _fallback_body(message)
    note = "P0 classifier budget cap hit; check manually"
    text = f"{note}\n\n{text}"
    html = f"<p><b>{escape(note)}</b></p>{html}"
    repository.mark_p0_review_candidate(session, message.chat_id, message.message_id)
    repository.mark_p0_classified(
        session,
        message.chat_id,
        message.message_id,
        "CAP_REVIEW",
        message.timestamp,
    )
    return _send_immediate_alert(
        session,
        message,
        email_sender,
        subject="[ПРОВЕРЬ] новое личное сообщение",
        body=text,
        html=html,
        alert_type="review_private",
    )


def handle_p0_candidate(
    session: Session,
    message: StoredMessage,
    llm: HaikuClient,
    email_sender: EmailSender,
    settings: Settings | None = None,
) -> bool:
    if message.is_outgoing:
        return False
    existing = repository.get_message(session, message.chat_id, message.message_id)
    if existing and existing.alert_sent:
        return False
    if existing and existing.p0_classified_at:
        return False

    if _is_non_text_media(message):
        repository.mark_p0_review_candidate(session, message.chat_id, message.message_id)
        if _is_private(message):
            body, html = _fallback_body(message)
            return _send_immediate_alert(
                session,
                message,
                email_sender,
                subject="[ПРОВЕРЬ] возможно важное личное сообщение",
                body=body,
                html=html,
                alert_type="review_private",
            )
        return False

    obvious = is_p0_candidate(message.text, message.caption)
    urgent_call = (
        message.chat_type == ChatType.private
        and is_urgent_call_candidate(message.text, message.caption)
    )
    if not _should_classify_immediately(session, message, settings, obvious or urgent_call):
        return False

    cap = _hourly_cap(settings)
    since = message.timestamp - timedelta(hours=1)
    if cap <= repository.p0_llm_calls_since(session, since):
        if _is_private(message):
            return _fail_open_budget_cap(session, message, email_sender)
        repository.mark_p0_review_candidate(session, message.chat_id, message.message_id)
        return False

    context = _context_with_reply_parent(session, message, _max_context_messages(settings))
    try:
        decision = llm.classify_p0(_message_payload(message, context, _max_message_chars(settings)))
        if decision.status == P0Status.not_p0:
            if not urgent_call:
                repository.mark_p0_classified(
                    session,
                    message.chat_id,
                    message.message_id,
                    P0Status.not_p0.value,
                    message.timestamp,
                )
                return False
            subject = f"[СРОЧНО] Telegram: {message.chat_title}"
            body = _decision_body(
                message,
                decision,
                reason="deterministic_urgent_call_override",
            )
            html = None
            alert_type = "p0"
        elif decision.status == P0Status.review:
            repository.mark_p0_review_candidate(session, message.chat_id, message.message_id)
            if message.chat_type == ChatType.private:
                subject = "[ПРОВЕРЬ] возможно важное личное сообщение"
                alert_type = "review_private"
            else:
                subject = f"[ПРОВЕРЬ] Telegram: {message.chat_title}"
                alert_type = "review_group"
            body, html = _fallback_body(message)
        else:
            subject = f"[СРОЧНО] Telegram: {message.chat_title}"
            body = _decision_body(message, decision)
            html = None
            alert_type = "p0"
        repository.mark_p0_classified(
            session,
            message.chat_id,
            message.message_id,
            decision.status.value if decision.status != P0Status.not_p0 else "P0",
            message.timestamp,
        )
    except LLMError:
        if message.chat_type == ChatType.private:
            subject = (
                f"[ВОЗМОЖНО СРОЧНО] Telegram: {message.chat_title}"
                if obvious
                else "[ПРОВЕРЬ] новое личное сообщение"
            )
            body, html = _fallback_body(message)
            alert_type = "p0" if obvious else "review_private"
        elif obvious:
            subject = f"[ВОЗМОЖНО СРОЧНО] Telegram: {message.chat_title}"
            body, html = _fallback_body(message)
            alert_type = "fallback_group_p0"
        else:
            repository.mark_p0_review_candidate(session, message.chat_id, message.message_id)
            return False

    return _send_immediate_alert(
        session,
        message,
        email_sender,
        subject=subject,
        body=body,
        html=html,
        alert_type=alert_type,
    )
