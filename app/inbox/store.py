from __future__ import annotations

import time
from datetime import datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError

from app.db.tables import InboxConversation, InboxNotification, LibrarySource
from app.inbox.library import SAVED_MESSAGES, peer_type_for_marked_id, source_token

ACTIVE_MINUTES = 5
LIFETIME = ACTIVE_MINUTES * 60

NOTIFICATION_REASONS = {
    "mention_only": "mention",
    "direct_reply": "direct_reply",
    "private_message": "private_message",
    "library_message": "library_message",
    "telegram_code": "telegram_code",
}

VIEW_LEASE_SECONDS = 15
MAX_MANUAL_WRITE_CHATS = 2


class ManualWriteLimitError(ValueError):
    pass


class InboxStore:
    def __init__(self, session_factory, ignored, clock=time.time):
        self.factory = session_factory
        self.ignored = ignored
        self.clock = clock
        # Foreground presence is intentionally process-local. A restart fails safe:
        # no stale browser tab can keep an attention projection open.
        self.view_leases: dict[str, float] = {}

    def refresh_view_lease(self, key):
        row = self.get(key)
        if row is None:
            return False
        self.view_leases[key] = self.clock() + VIEW_LEASE_SECONDS
        return True

    def release_view_lease(self, key):
        self.view_leases.pop(key, None)

    def has_view_lease(self, key):
        deadline = self.view_leases.get(key, 0)
        if deadline <= self.clock():
            self.view_leases.pop(key, None)
            return False
        return True

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
                InboxConversation.quarantined_at.is_(None),
                InboxConversation.peer_id.not_in(self.ignored()),
            ).order_by(InboxConversation.activated_at.desc())))

    def get(self, key):
        return next((row for row in self.active() if row.id == key), None)

    def get_any(self, key):
        with self.factory() as session:
            return session.get(InboxConversation, key)

    def unread_total(self, rows=None):
        return sum(max(0, int(row.unread_count or 0)) for row in (
            self.active() if rows is None else rows
        ))

    def activate(
        self,
        *,
        peer_id,
        thread_id,
        is_forum,
        title,
        trigger_id,
        preview,
        reason=None,
        peer_type=None,
        access_hash=None,
        library_source_id=None,
        notifications_muted=False,
    ):
        """Upsert one attention projection and count each Telegram message once.

        SQLite's unique constraint is the final arbiter. A concurrent insert retries
        against the winner instead of losing the second message or creating a row.
        """
        now = self.clock()
        peer_id = str(int(peer_id))
        peer_type = peer_type or peer_type_for_marked_id(peer_id)
        thread_id = 0 if peer_type == "user" else int(thread_id or 0)
        if peer_id in self.ignored():
            return None

        for attempt in range(2):
            try:
                with self.factory() as session:
                    # SQLite deferred transactions may otherwise let two writers
                    # read the same unread/latest state and later overwrite one
                    # another. Acquire the writer slot before reading the row.
                    if session.get_bind().dialect.name == "sqlite":
                        session.execute(text("BEGIN IMMEDIATE"))
                    row = session.scalar(select(InboxConversation).where(
                        InboxConversation.peer_type == peer_type,
                        InboxConversation.peer_id == peer_id,
                        InboxConversation.thread_id == thread_id,
                    ))
                    created = row is None
                    if created:
                        result = session.execute(sqlite_insert(InboxConversation).values(
                            id=uuid4().hex,
                            peer_type=peer_type,
                            peer_id=peer_id,
                            access_hash=str(access_hash) if access_hash is not None else None,
                            thread_id=thread_id,
                            is_forum=bool(is_forum),
                            title=title[:512],
                            topic_title="",
                            preview="",
                            trigger_id=0,
                            activated_at=now,
                            opened_at=None,
                            latest_relevant_message_id=0,
                            last_seen_message_id=0,
                            unread_count=0,
                            expires_at=0,
                            manually_closed=False,
                            library_source_id=library_source_id,
                            quarantined_at=None,
                            quarantine_reason=None,
                        ).on_conflict_do_nothing(index_elements=[
                            "peer_type", "peer_id", "thread_id",
                        ]))
                        created = result.rowcount == 1
                        row = session.scalar(select(InboxConversation).where(
                            InboxConversation.peer_type == peer_type,
                            InboxConversation.peer_id == peer_id,
                            InboxConversation.thread_id == thread_id,
                        ))
                        if row is None:
                            # Defensive retry for an exotic driver where a
                            # concurrent winner is not visible until transaction
                            # restart. SQLite normally serializes the insert.
                            session.rollback()
                            continue

                    previous_latest = max(
                        int(row.latest_relevant_message_id or 0), int(row.trigger_id or 0)
                    )
                    next_unread = max(0, int(row.unread_count or 0)) + 1
                    inserted_event = None
                    foreground = self.has_view_lease(row.id)
                    public_reason = NOTIFICATION_REASONS.get(reason)
                    if public_reason is not None:
                        result = session.execute(sqlite_insert(InboxNotification).values(
                            peer_id=peer_id,
                            trigger_id=trigger_id,
                            conversation_id=row.id,
                            title=plain_preview(title, 160),
                            topic_title=plain_preview(row.topic_title or "", 160),
                            preview=plain_preview(preview, 240),
                            trigger_reason=public_reason,
                            unread_count=next_unread,
                            suppressed=bool(notifications_muted or foreground),
                            created_at=now,
                        ).on_conflict_do_nothing(
                            index_elements=["peer_id", "trigger_id"]
                        ))
                        inserted_event = result.rowcount == 1

                    # Legacy/internal callers without a notification reason still
                    # retain monotonic replay protection.
                    event_is_new = (
                        inserted_event if inserted_event is not None
                        else int(trigger_id) > previous_latest
                    )
                    if created and inserted_event is False:
                        # A retained notification tombstone proves this is a replay
                        # of a projection that already expired or was closed.
                        session.delete(row)
                        session.commit()
                        return None

                    if access_hash is not None:
                        row.access_hash = str(access_hash)
                    row.peer_type = peer_type
                    row.is_forum = bool(is_forum)
                    # The association describes the current classification, not
                    # the peer forever.  Clearing it is essential when a source
                    # is deselected and a later ordinary mention reuses the same
                    # canonical conversation row.
                    row.library_source_id = library_source_id
                    row.quarantined_at = None
                    row.quarantine_reason = None
                    row.latest_relevant_message_id = max(previous_latest, int(trigger_id))

                    unseen = event_is_new and int(trigger_id) > int(
                        row.last_seen_message_id or 0
                    )
                    if unseen:
                        pending = not foreground or (
                            row.opened_at is None
                            or row.manually_closed
                            or row.expires_at <= now
                        )
                        row.activated_at = now
                        if pending:
                            row.unread_count = next_unread
                            row.opened_at = None
                            row.expires_at = 0
                        else:
                            row.unread_count = 0
                            row.last_seen_message_id = max(
                                int(row.last_seen_message_id or 0), int(trigger_id)
                            )
                            row.expires_at = now + LIFETIME
                        row.manually_closed = False
                    if int(trigger_id) >= int(row.trigger_id or 0):
                        row.title = title[:512]
                        row.preview = preview[:256]
                        row.trigger_id = int(trigger_id)
                        if event_is_new:
                            row.activated_at = now
                    session.commit()
                    return row
            except IntegrityError:
                if attempt:
                    raise
        return None

    def open(self, key):
        now = self.clock()
        with self.factory() as session:
            row = session.get(InboxConversation, key)
            if (
                row is not None
                and row.quarantined_at is None
                and not row.manually_closed
                and row.peer_id not in self.ignored()
                and (row.opened_at is None or row.expires_at > now)
            ):
                if row.opened_at is None:
                    row.opened_at = now
                    row.expires_at = now + LIFETIME
                row.last_seen_message_id = row.latest_relevant_message_id
                row.unread_count = 0
                if row.library_source_id:
                    source = session.get(LibrarySource, row.library_source_id)
                    if source:
                        source.last_seen_message_id = max(
                            int(source.last_seen_message_id or 0),
                            int(row.latest_relevant_message_id or 0),
                        )
                        source.updated_at = now
                session.commit()
        return self.get(key)

    def close(self, key):
        self.release_view_lease(key)
        with self.factory() as session:
            row = session.get(InboxConversation, key)
            if row:
                row.manually_closed = True
                row.preview = ""
                session.commit()

    def extend(self, key):
        with self.factory() as session:
            row = session.get(InboxConversation, key)
            if (
                row
                and row.quarantined_at is None
                and not row.manually_closed
                and row.peer_id not in self.ignored()
            ):
                row.opened_at = row.opened_at if row.opened_at is not None else self.clock()
                row.expires_at = self.clock() + LIFETIME
                session.commit()

    def quarantine(self, key, reason="invalid_peer"):
        with self.factory() as session:
            row = session.get(InboxConversation, key)
            if row:
                row.quarantined_at = self.clock()
                row.quarantine_reason = reason[:32]
                row.manually_closed = True
                row.preview = ""
                session.commit()

    def remember_peer(self, peer_type, peer_id, access_hash, *, title=None, is_bot=None):
        peer_id = str(int(peer_id))
        with self.factory() as session:
            values = {"peer_type": peer_type}
            if access_hash is not None:
                values["access_hash"] = str(access_hash)
            session.execute(update(InboxConversation).where(
                InboxConversation.peer_id == peer_id,
            ).values(**values))
            source = session.scalar(select(LibrarySource).where(
                LibrarySource.peer_type == peer_type,
                LibrarySource.peer_id == peer_id,
            ))
            if source:
                if access_hash is not None:
                    source.access_hash = str(access_hash)
                if title:
                    source.display_title = title[:512]
                if is_bot is not None:
                    source.is_bot = bool(is_bot)
                source.updated_at = self.clock()
            session.commit()

    def notifications(self, after=None):
        # Cursor advances over muted, seen, ignored and otherwise suppressed rows.
        with self.factory() as session:
            latest = session.scalar(select(func.max(InboxNotification.id))) or 0
            if after is None or after > latest:
                return {"events": [], "cursor": latest}
            rows = list(session.scalars(select(InboxNotification).where(
                InboxNotification.id > after,
                InboxNotification.id <= latest,
            ).order_by(InboxNotification.id).limit(100)))
            active = {row.id: row for row in self.active()}
            source_ids = {
                row.library_source_id for row in active.values() if row.library_source_id
            }
            muted = {
                row.id for row in session.scalars(select(LibrarySource).where(
                    LibrarySource.id.in_(source_ids),
                    LibrarySource.notifications_muted.is_(True),
                ))
            } if source_ids else set()
            events = []
            for row in rows:
                conversation = active.get(row.conversation_id)
                if (
                    conversation is None
                    or row.peer_id in self.ignored()
                    or row.created_at < self.clock() - 86400
                    or row.suppressed
                    or row.trigger_id <= conversation.last_seen_message_id
                    or conversation.library_source_id in muted
                ):
                    continue
                events.append({
                    "event_id": row.id,
                    "conversation_id": row.conversation_id,
                    "title": row.title,
                    "topic_title": plain_preview(
                        conversation.topic_title or row.topic_title, 160
                    ),
                    "preview": row.preview,
                    "trigger_reason": row.trigger_reason,
                    "unread_count": row.unread_count,
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
            # Expired rows are durable canonical identities. ``active`` hides them,
            # while a later message reuses the same conversation ID and read state.
            session.execute(delete(InboxConversation).where(
                InboxConversation.peer_id.in_(self.ignored())
            ))
            session.commit()

    # --- Persistent Library preferences -------------------------------------------------

    @staticmethod
    def _associate_library_source(session, source):
        peer_rows = (
            InboxConversation.peer_type == source.peer_type,
            InboxConversation.peer_id == source.peer_id,
        )
        session.execute(update(InboxConversation).where(*peer_rows).values(
            library_source_id=source.id
        ))
        # A selected source has exactly one peer-wide (thread 0) projection.
        # Retain a pre-existing peer-wide card, but close topic/discussion cards;
        # the next source update will activate or reuse the canonical thread 0.
        session.execute(update(InboxConversation).where(
            *peer_rows, InboxConversation.thread_id != 0,
        ).values(manually_closed=True, preview=""))

    def seed_library(self, chats):
        """Import the old operator JSON once without any Telegram startup scan."""
        now = self.clock()
        with self.factory() as session:
            existing_count = session.scalar(select(func.count(LibrarySource.id))) or 0
            next_order = session.scalar(select(func.max(LibrarySource.sort_order))) or 0
            for offset, chat in enumerate(chats):
                if chat.id == SAVED_MESSAGES.id:
                    continue
                peer_id = str(int(chat.peer_id))
                peer_type = peer_type_for_marked_id(peer_id)
                source = session.scalar(select(LibrarySource).where(
                    LibrarySource.peer_type == peer_type,
                    LibrarySource.peer_id == peer_id,
                ))
                if source is not None:
                    if source.library_enabled:
                        self._associate_library_source(session, source)
                    continue
                source_id = chat.id or source_token(peer_id)
                if session.get(LibrarySource, source_id) is not None:
                    source_id = uuid4().hex
                source = LibrarySource(
                    id=source_id,
                    peer_type=peer_type,
                    peer_id=peer_id,
                    access_hash=None,
                    display_title=chat.title[:512],
                    library_enabled=True,
                    sort_order=(offset + 1 if not existing_count else next_order + offset + 1),
                    notifications_muted=False,
                    allow_bot_write=False,
                    digest_excluded=False,
                    manual_write_enabled=False,
                    manual_open_date=None,
                    manual_access_until=0,
                    last_seen_message_id=0,
                    is_bot=False,
                    created_at=now,
                    updated_at=now,
                )
                session.add(source)
                session.flush()
                self._associate_library_source(session, source)
            session.commit()

    def library_sources(self, *, enabled_only=True):
        with self.factory() as session:
            query = select(LibrarySource)
            if enabled_only:
                query = query.where(LibrarySource.library_enabled.is_(True))
            return list(session.scalars(query.order_by(
                LibrarySource.sort_order, LibrarySource.created_at, LibrarySource.id
            )))

    def library_source(self, source_id, *, enabled_only=True):
        with self.factory() as session:
            source = session.get(LibrarySource, source_id)
            if source is None or (enabled_only and not source.library_enabled):
                return None
            return source

    def library_source_for_peer(self, peer_id, *, enabled_only=True):
        peer_id = str(int(peer_id))
        with self.factory() as session:
            query = select(LibrarySource).where(LibrarySource.peer_id == peer_id)
            if enabled_only:
                query = query.where(LibrarySource.library_enabled.is_(True))
            return session.scalar(query)

    def upsert_library_source(
        self,
        *,
        peer_type,
        peer_id,
        access_hash,
        display_title,
        is_bot,
        source_id=None,
        library_enabled=True,
        notifications_muted=False,
        allow_bot_write=False,
        digest_excluded=False,
        manual_write_enabled=False,
        sort_order=None,
    ):
        peer_id = str(int(peer_id))
        now = self.clock()
        with self.factory() as session:
            if session.get_bind().dialect.name == "sqlite":
                session.execute(text("BEGIN IMMEDIATE"))
            if manual_write_enabled:
                selected = session.scalar(select(func.count(LibrarySource.id)).where(
                    LibrarySource.manual_write_enabled.is_(True),
                    LibrarySource.peer_id != peer_id,
                )) or 0
                if selected >= MAX_MANUAL_WRITE_CHATS:
                    raise ManualWriteLimitError("manual write limit reached")
            source = session.scalar(select(LibrarySource).where(
                LibrarySource.peer_type == peer_type,
                LibrarySource.peer_id == peer_id,
            ))
            if source is None:
                source_id = source_id or uuid4().hex
                if session.get(LibrarySource, source_id) is not None:
                    source_id = uuid4().hex
                if sort_order is None:
                    sort_order = (session.scalar(select(func.max(
                        LibrarySource.sort_order
                    ))) or 0) + 1
                source = LibrarySource(
                    id=source_id,
                    peer_type=peer_type,
                    peer_id=peer_id,
                    access_hash=str(access_hash) if access_hash is not None else None,
                    display_title=display_title[:512],
                    library_enabled=bool(library_enabled),
                    sort_order=int(sort_order),
                    notifications_muted=bool(notifications_muted),
                    allow_bot_write=bool(allow_bot_write and is_bot),
                    digest_excluded=bool(digest_excluded),
                    manual_write_enabled=bool(manual_write_enabled),
                    manual_open_date=None,
                    manual_access_until=0,
                    last_seen_message_id=0,
                    is_bot=bool(is_bot),
                    created_at=now,
                    updated_at=now,
                )
                session.add(source)
            else:
                source.access_hash = (
                    str(access_hash) if access_hash is not None else source.access_hash
                )
                source.display_title = display_title[:512]
                source.is_bot = bool(is_bot)
                source.library_enabled = bool(library_enabled)
                source.notifications_muted = bool(notifications_muted)
                source.allow_bot_write = bool(allow_bot_write and is_bot)
                source.digest_excluded = bool(digest_excluded)
                source.manual_write_enabled = bool(manual_write_enabled)
                if sort_order is not None:
                    source.sort_order = int(sort_order)
                source.updated_at = now
            session.flush()
            if source.library_enabled:
                self._associate_library_source(session, source)
            else:
                session.execute(update(InboxConversation).where(
                    InboxConversation.library_source_id == source.id,
                ).values(manually_closed=True, preview=""))
            session.commit()
            return source

    def update_library_source(self, source_id, **values):
        allowed = {
            "library_enabled", "sort_order", "notifications_muted",
            "allow_bot_write", "digest_excluded", "manual_write_enabled",
            "display_title", "access_hash", "is_bot",
        }
        if set(values) - allowed:
            raise ValueError("unsupported library preference")
        with self.factory() as session:
            if session.get_bind().dialect.name == "sqlite":
                session.execute(text("BEGIN IMMEDIATE"))
            source = session.get(LibrarySource, source_id)
            if source is None:
                return None
            if values.get("manual_write_enabled") and not source.manual_write_enabled:
                selected = session.scalar(select(func.count(LibrarySource.id)).where(
                    LibrarySource.manual_write_enabled.is_(True)
                )) or 0
                if selected >= MAX_MANUAL_WRITE_CHATS:
                    raise ManualWriteLimitError("manual write limit reached")
            for key, value in values.items():
                setattr(source, key, value)
            if source.allow_bot_write and not source.is_bot:
                source.allow_bot_write = False
            source.updated_at = self.clock()
            if source.library_enabled:
                self._associate_library_source(session, source)
            else:
                session.execute(update(InboxConversation).where(
                    InboxConversation.library_source_id == source.id,
                ).values(manually_closed=True, preview=""))
            session.commit()
            return source

    def manual_write_sources(self):
        with self.factory() as session:
            return list(session.scalars(select(LibrarySource).where(
                LibrarySource.manual_write_enabled.is_(True)
            ).order_by(LibrarySource.updated_at, LibrarySource.id)))

    def open_manual_write(self, source_id, timezone):
        now = self.clock()
        zone = ZoneInfo(timezone)
        local_now = datetime.fromtimestamp(now, zone)
        today = local_now.date()
        tomorrow = datetime.combine(today + timedelta(days=1), datetime.min.time(), zone)
        retry_after = max(1, int(tomorrow.timestamp() - now))
        with self.factory() as session:
            if session.get_bind().dialect.name == "sqlite":
                session.execute(text("BEGIN IMMEDIATE"))
            source = session.get(LibrarySource, source_id)
            if source is None or not source.manual_write_enabled:
                return None, None
            if source.manual_open_date == today:
                if float(source.manual_access_until or 0) <= now:
                    return source, retry_after
            else:
                source.manual_open_date = today
                source.manual_access_until = now + LIFETIME
                source.updated_at = now
                session.commit()
            return source, 0

    def manual_write_source(self, source_id, *, require_access=False):
        with self.factory() as session:
            source = session.get(LibrarySource, source_id)
            if source is None or not source.manual_write_enabled:
                return None
            if require_access and float(source.manual_access_until or 0) <= self.clock():
                return None
            return source

    def extend_manual_write(self, source_id):
        with self.factory() as session:
            source = session.get(LibrarySource, source_id)
            if source is None or not source.manual_write_enabled:
                return None
            source.manual_access_until = self.clock() + LIFETIME
            source.updated_at = self.clock()
            session.commit()
            return source

    def reorder_library(self, source_ids):
        if len(source_ids) != len(set(source_ids)):
            return False
        with self.factory() as session:
            enabled = list(session.scalars(select(LibrarySource).where(
                LibrarySource.library_enabled.is_(True)
            )))
            if {row.id for row in enabled} != set(source_ids):
                return False
            positions = {source_id: index + 1 for index, source_id in enumerate(source_ids)}
            for row in enabled:
                row.sort_order = positions[row.id]
                row.updated_at = self.clock()
            session.commit()
            return True

    def mark_library_seen(self, source_id, latest_message_id):
        with self.factory() as session:
            source = session.get(LibrarySource, source_id)
            if source is None or not source.library_enabled:
                return None
            source.last_seen_message_id = max(
                int(source.last_seen_message_id or 0), int(latest_message_id or 0)
            )
            source.updated_at = self.clock()
            rows = session.scalars(select(InboxConversation).where(
                InboxConversation.library_source_id == source_id,
                InboxConversation.quarantined_at.is_(None),
            ))
            for row in rows:
                row.last_seen_message_id = max(
                    int(row.last_seen_message_id or 0), int(row.latest_relevant_message_id or 0)
                )
                row.unread_count = 0
            session.commit()
            return source


def plain_preview(value, limit):
    return " ".join("".join(c if c.isprintable() else " " for c in value).split())[:limit]
