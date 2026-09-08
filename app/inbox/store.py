from __future__ import annotations

import time
from uuid import uuid4

from sqlalchemy import delete, func, select, update

from app.db.tables import InboxConversation, InboxNotification

ACTIVE_MINUTES = 5
LIFETIME = ACTIVE_MINUTES * 60


class InboxStore:
    def __init__(self, session_factory, ignored, clock=time.time):
        self.factory = session_factory
        self.ignored = ignored
        self.clock = clock

    def clamp_existing_lifetimes(self):
        # Upgrade existing hour-long windows without extending any short window.
        with self.factory() as session:
            session.execute(update(InboxConversation).where(
                InboxConversation.expires_at > self.clock() + LIFETIME,
            ).values(expires_at=self.clock() + LIFETIME))
            session.commit()

    def active(self):
        with self.factory() as session:
            return list(session.scalars(select(InboxConversation).where(
                (InboxConversation.opened_at.is_(None)
                 | (InboxConversation.expires_at > self.clock())),
                InboxConversation.manually_closed.is_(False),
                InboxConversation.peer_id.not_in(self.ignored()),
            ).order_by(InboxConversation.activated_at.desc())))

    def get(self, key):
        return next((row for row in self.active() if row.id == key), None)

    def activate(self, *, peer_id, thread_id, is_forum, title, trigger_id, preview, reason=None):
        now = self.clock()
        if str(peer_id) in self.ignored():
            return None
        with self.factory() as session:
            row = session.scalar(select(InboxConversation).where(
                InboxConversation.peer_id == str(peer_id),
                InboxConversation.thread_id == thread_id,
            ))
            if row and trigger_id <= row.trigger_id:
                self.record_notification(session, row, trigger_id, title, preview, reason, now)
                session.commit()
                return row  # Replayed updates never reopen a manually closed conversation.
            if row is None:
                row = InboxConversation(id=uuid4().hex, peer_id=str(peer_id), thread_id=thread_id)
                session.add(row)
            pending = (row.opened_at is None or row.manually_closed
                       or row.expires_at <= now)
            row.is_forum = is_forum
            row.title = title[:512]
            row.preview = preview[:256]
            row.trigger_id = trigger_id
            row.activated_at = now
            row.opened_at = None if pending else row.opened_at
            row.expires_at = 0 if pending else now + LIFETIME
            row.manually_closed = False
            self.record_notification(session, row, trigger_id, title, preview, reason, now)
            session.commit()
            return row

    @staticmethod
    def record_notification(session, row, trigger_id, title, preview, reason, now):
        if reason not in {"mention_only", "direct_reply"}:
            return
        existing = session.scalar(select(InboxNotification.id).where(
            InboxNotification.peer_id == str(row.peer_id),
            InboxNotification.trigger_id == trigger_id,
        ))
        if existing is None:
            session.add(InboxNotification(
                peer_id=str(row.peer_id), trigger_id=trigger_id, conversation_id=row.id,
                title=plain_preview(title, 160),
                topic_title=plain_preview(row.topic_title or "", 160),
                preview=plain_preview(preview, 240),
                trigger_reason="mention" if reason == "mention_only" else "direct_reply",
                created_at=now,
            ))

    def open(self, key):
        now = self.clock()
        with self.factory() as session:
            session.execute(update(InboxConversation).where(
                InboxConversation.id == key,
                InboxConversation.opened_at.is_(None),
                InboxConversation.manually_closed.is_(False),
                InboxConversation.peer_id.not_in(self.ignored()),
            ).values(opened_at=now, expires_at=now + LIFETIME))
            session.commit()
        return self.get(key)

    def close(self, key):
        with self.factory() as session:
            row = session.get(InboxConversation, key)
            if row:
                row.manually_closed = True
                row.preview = ""
                session.commit()

    def extend(self, key):
        with self.factory() as session:
            row = session.get(InboxConversation, key)
            if row and not row.manually_closed and row.peer_id not in self.ignored():
                row.opened_at = row.opened_at if row.opened_at is not None else self.clock()
                row.expires_at = self.clock() + LIFETIME
                session.commit()

    def notifications(self, after=None):
        # Cursor advances over suppressed rows too. No lifecycle writes.
        with self.factory() as session:
            latest = session.scalar(select(func.max(InboxNotification.id))) or 0
            if after is None or after > latest:
                return {"events": [], "cursor": latest}
            rows = list(session.scalars(select(InboxNotification).where(
                InboxNotification.id > after, InboxNotification.id <= latest,
            ).order_by(InboxNotification.id).limit(100)))
            active = {row.id: row for row in self.active()}
            events = []
            for row in rows:
                conversation = active.get(row.conversation_id)
                if (conversation is None or row.peer_id in self.ignored()
                        or row.created_at < self.clock() - 86400):
                    continue
                events.append({
                    "event_id": row.id, "conversation_id": row.conversation_id,
                    "title": row.title, "topic_title": plain_preview(
                        conversation.topic_title or row.topic_title, 160),
                    "preview": row.preview, "trigger_reason": row.trigger_reason,
                    "created_at": row.created_at,
                })
            return {"events": events, "cursor": rows[-1].id if rows else latest}

    def cleanup(self):
        with self.factory() as session:
            # Keep compact dedup/cursor tombstones, discard notification text after one day.
            session.execute(update(InboxNotification).where(
                (InboxNotification.created_at < self.clock() - 86400)
                | InboxNotification.peer_id.in_(self.ignored()),
                (InboxNotification.title != "") | (InboxNotification.preview != "")
                | (InboxNotification.topic_title != ""),
            ).values(title="", topic_title="", preview=""))
            session.execute(delete(InboxConversation).where(
                (InboxConversation.opened_at.is_not(None)
                 & (InboxConversation.expires_at <= self.clock()))
                | InboxConversation.peer_id.in_(self.ignored())
            ))
            session.commit()


def plain_preview(value, limit):
    return " ".join("".join(c if c.isprintable() else " " for c in value).split())[:limit]
