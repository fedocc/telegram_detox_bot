from __future__ import annotations

import json
from datetime import UTC

from app.db.tables import InboxNotification

DIGEST_TARGET = "digest"
DIGEST_READY = "digest_ready"
DIGEST_URL = "/?view=digests"


def is_digest_notification(event):
    return (event.trigger_reason == DIGEST_READY
            and event.conversation_id == DIGEST_TARGET
            and event.peer_id == DIGEST_TARGET)


def digest_notification(record):
    count = len(json.loads(record.payload_json).get("items", []))
    if count:
        noun = ("пункт" if count % 10 == 1 and count % 100 != 11 else
                "пункта" if count % 10 in {2, 3, 4} and count % 100 not in {12, 13, 14}
                else "пунктов")
        preview = f"{count} {noun}. Нажмите, чтобы открыть."
    else:
        preview = "За день ничего важного. Нажмите, чтобы открыть."
    return InboxNotification(
        peer_id=DIGEST_TARGET, trigger_id=record.id, conversation_id=DIGEST_TARGET,
        title="Сводка готова", topic_title="", preview=preview,
        trigger_reason=DIGEST_READY, unread_count=1, suppressed=False,
        created_at=record.generated_at.replace(tzinfo=UTC).timestamp(),
    )
