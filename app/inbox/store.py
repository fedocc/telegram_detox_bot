from __future__ import annotations

import time
from uuid import uuid4

from sqlalchemy import delete, select, update

from app.db.tables import InboxConversation

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
            session.commit()
            return row

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

    def cleanup(self):
        with self.factory() as session:
            session.execute(delete(InboxConversation).where(
                (InboxConversation.opened_at.is_not(None)
                 & (InboxConversation.expires_at <= self.clock()))
                | InboxConversation.peer_id.in_(self.ignored())
            ))
            session.commit()
