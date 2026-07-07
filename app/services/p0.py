from __future__ import annotations

from html import escape

from sqlalchemy.orm import Session

from app.db import repository
from app.email.sender import EmailSender
from app.llm.client import HaikuClient, LLMError
from app.models.schemas import ChatType, P0Status, StoredMessage
from app.services.digest import safe_truncate
from app.services.prefilter import is_p0_candidate, is_urgent_call_candidate

SAFE_TEXT_LIMIT = 500


def _message_payload(message: StoredMessage, context: list) -> dict:
    capped_text = safe_truncate(message.text or message.caption, SAFE_TEXT_LIMIT)
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


def _context_with_reply_parent(session: Session, message: StoredMessage) -> list:
    context = repository.recent_chat_context(session, message.chat_id, limit=6)
    if message.reply_to_message_id:
        parent = repository.get_message(session, message.chat_id, message.reply_to_message_id)
        if parent and all(row.message_id != parent.message_id for row in context):
            context.insert(0, parent)
    return context[-6:]


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


def handle_p0_candidate(
    session: Session,
    message: StoredMessage,
    llm: HaikuClient,
    email_sender: EmailSender,
) -> bool:
    if message.is_outgoing:
        return False
    existing = repository.get_message(session, message.chat_id, message.message_id)
    if existing and existing.alert_sent:
        return False

    obvious = is_p0_candidate(message.text, message.caption)
    urgent_call = (
        message.chat_type == ChatType.private
        and is_urgent_call_candidate(message.text, message.caption)
    )
    context = _context_with_reply_parent(session, message)
    try:
        decision = llm.classify_p0(_message_payload(message, context))
        if decision.status == P0Status.not_p0:
            if not urgent_call:
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
            if message.chat_type == ChatType.private:
                subject = "[ПРОВЕРЬ] возможно важное личное сообщение"
                body, html = _fallback_body(message)
                alert_type = "review_private"
            else:
                repository.mark_p0_review_candidate(session, message.chat_id, message.message_id)
                return False
        else:
            subject = f"[СРОЧНО] Telegram: {message.chat_title}"
            body = _decision_body(message, decision)
            html = None
            alert_type = "p0"
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
