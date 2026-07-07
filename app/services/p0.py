from __future__ import annotations

from html import escape

from sqlalchemy.orm import Session

from app.db import repository
from app.email.sender import EmailSender
from app.llm.client import HaikuClient, LLMError
from app.models.schemas import ChatType, P0Status, StoredMessage
from app.services.digest import safe_truncate
from app.services.prefilter import is_p0_candidate

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
    context = _context_with_reply_parent(session, message)
    try:
        decision = llm.classify_p0(_message_payload(message, context))
        if decision.status == P0Status.not_p0:
            return False
        if decision.status == P0Status.review:
            if message.chat_type == ChatType.private:
                subject = "[ПРОВЕРЬ] возможно важное личное сообщение"
                body, html = _fallback_body(message)
            else:
                repository.mark_p0_review_candidate(session, message.chat_id, message.message_id)
                return False
        else:
            subject = f"[СРОЧНО] Telegram: {message.chat_title}"
            body = (
                f"{message.sender_name or 'Unknown'}: {decision.summary}"
                f"\n\nДействие: {decision.action or '-'}"
            )
            html = None
    except LLMError:
        if message.chat_type == ChatType.private:
            subject = (
                f"[ВОЗМОЖНО СРОЧНО] Telegram: {message.chat_title}"
                if obvious
                else "[ПРОВЕРЬ] новое личное сообщение"
            )
            body, html = _fallback_body(message)
        elif obvious:
            subject = f"[ВОЗМОЖНО СРОЧНО] Telegram: {message.chat_title}"
            body, html = _fallback_body(message)
        else:
            repository.mark_p0_review_candidate(session, message.chat_id, message.message_id)
            return False

    email_sender.send(subject, body, html)
    repository.mark_alert_sent(session, message.chat_id, message.message_id)
    return True
