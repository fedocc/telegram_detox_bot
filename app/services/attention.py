"""Deterministic attention triggers, shared by ingestion, inbox and durable retry."""
from __future__ import annotations

from app.services.mentions import has_exact_fedocc_mention
from app.telegram.mapper import resolve_reply_to_is_mine

DETERMINISTIC_ALERT_TYPES = frozenset({"mention_only", "direct_reply"})


def trigger_from_evidence(text, *, outgoing, reply_to_is_mine=False, reply_id=None):
    if outgoing:
        return None
    # One message always has one type, even when both conditions match.
    if has_exact_fedocc_mention(text):
        return "mention_only"
    if reply_to_is_mine is True and reply_id:
        return "direct_reply"
    return None


async def classify_incoming(message, *, self_id=None, text=None, outgoing=False, sender_id=None):
    if outgoing or getattr(message, "out", False):
        return None
    author = sender_id if sender_id is not None else getattr(message, "sender_id", None)
    if self_id is not None and author == self_id:
        return None
    body = text if text is not None else getattr(message, "raw_text", None)
    if has_exact_fedocc_mention(body):
        return "mention_only"
    # The resolver checks reply_to_msg_id before making any Telegram request.
    mine = await resolve_reply_to_is_mine(message, self_id=self_id)
    return trigger_from_evidence(body, outgoing=False, reply_to_is_mine=mine,
                                 reply_id=getattr(message, "reply_to_msg_id", None))
