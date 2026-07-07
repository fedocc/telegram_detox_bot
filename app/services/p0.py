from __future__ import annotations

from sqlalchemy.orm import Session

from app.db import repository
from app.email.sender import EmailSender
from app.llm.client import HaikuClient, LLMError
from app.models.schemas import StoredMessage
from app.services.prefilter import is_p0_candidate


def _message_payload(message: StoredMessage, context: list) -> dict:
    return {
        "message": message.model_dump(mode="json"),
        "context": [
            {
                "sender": row.sender_name,
                "is_outgoing": row.is_outgoing,
                "text": row.text or row.caption,
                "message_id": row.message_id,
            }
            for row in context
        ],
    }


def handle_p0_candidate(
    session: Session,
    message: StoredMessage,
    llm: HaikuClient,
    email_sender: EmailSender,
) -> bool:
    if message.is_outgoing or not is_p0_candidate(message.text, message.caption):
        return False
    existing = repository.get_message(session, message.chat_id, message.message_id)
    if existing and existing.alert_sent:
        return False

    context = repository.recent_chat_context(session, message.chat_id, limit=10)
    try:
        decision = llm.classify_p0(_message_payload(message, context))
        if not decision.is_p0:
            return False
        subject = f"[СРОЧНО] Telegram: {message.chat_title}"
        body = (
            f"{message.sender_name or 'Unknown'}: {decision.summary}"
            f"\n\nДействие: {decision.action or '-'}"
        )
    except LLMError:
        subject = f"[ВОЗМОЖНО СРОЧНО] Telegram: {message.chat_title}"
        body = (
            f"{message.sender_name or 'Unknown'}: "
            f"{message.text or message.caption or '[media без текста]'}"
        )

    email_sender.send(subject, body, None)
    repository.mark_alert_sent(session, message.chat_id, message.message_id)
    return True
