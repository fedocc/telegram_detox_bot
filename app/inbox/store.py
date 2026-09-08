from __future__ import annotations

import time
from uuid import uuid4

from sqlalchemy import delete, select

from app.db.tables import InboxConversation

LIFETIME = 3600


class InboxStore:
    def __init__(self, session_factory, ignored, clock=time.time):
        self.factory = session_factory
        self.ignored = ignored
        self.clock = clock

    def active(self):
        with self.factory() as session:
            return list(session.scalars(select(InboxConversation).where(
                InboxConversation.expires_at > self.clock(),
                InboxConversation.manually_closed.is_(False),
                InboxConversation.peer_id.not_in(self.ignored()),
            ).order_by(InboxConversation.activated_at.desc())))

    def get(self, key):
        return next((row for row in self.active() if row.id == key), None)

    def activate(self, *, peer_id, thread_id, is_forum, title, trigger_id, preview):
        now = self.clock()
        if str(peer_id) in self.ignored():
            return None
        with self.factory() as session:
            row = session.scalar(select(InboxConversation).where(
                InboxConversation.peer_id == str(peer_id),
                InboxConversation.thread_id == thread_id,
            ))
            if row and trigger_id <= row.trigger_id:
                return row  # Replayed updates never reopen a manually closed conversation.
            if row is None:
                row = InboxConversation(id=uuid4().hex, peer_id=str(peer_id), thread_id=thread_id)
                session.add(row)
            row.is_forum = is_forum
            row.title = title[:512]
            row.preview = preview[:256]
            row.trigger_id = trigger_id
            row.activated_at = now
            row.expires_at = now + LIFETIME
            row.manually_closed = False
            session.commit()
            return row

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
                row.expires_at = self.clock() + LIFETIME
                session.commit()

    def cleanup(self):
        with self.factory() as session:
            session.execute(delete(InboxConversation).where(
                (InboxConversation.expires_at <= self.clock())
                | InboxConversation.peer_id.in_(self.ignored())
            ))
            session.commit()
