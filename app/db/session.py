from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.db.tables import Base

CHANNEL_MARK = 1_000_000_000_000


def _canonical_peer(value: object) -> tuple[str, str]:
    """Return an explicit Telegram peer type and canonical marked ID."""
    try:
        marked = int(str(value))
    except (TypeError, ValueError):
        return "unknown", str(value)
    if marked > 0:
        peer_type = "user"
    elif marked <= -CHANNEL_MARK:
        peer_type = "channel"
    else:
        peer_type = "chat"
    return peer_type, str(marked)


def _merge_inbox_duplicates(connection) -> None:
    """Normalize legacy identities and merge rows without losing durable references."""
    rows = list(connection.execute(text(
        "SELECT id, peer_type, peer_id, access_hash, thread_id, is_forum, title, "
        "topic_title, preview, trigger_id, activated_at, opened_at, "
        "latest_relevant_message_id, last_seen_message_id, unread_count, expires_at, "
        "manually_closed, library_source_id, quarantined_at, quarantine_reason "
        "FROM inbox_conversations"
    )).mappings())
    if not rows:
        return

    notification_rows = list(connection.execute(text(
        "SELECT id, peer_id, trigger_id, conversation_id, title, topic_title, preview, "
        "trigger_reason, unread_count, suppressed, created_at FROM inbox_notifications"
    )).mappings())
    notifications_by_conversation: dict[str, list[dict]] = {}
    for item in notification_rows:
        notifications_by_conversation.setdefault(item["conversation_id"], []).append(item)

    groups: dict[tuple[str, str, int], list] = {}
    for row in rows:
        inferred_type, canonical_peer = _canonical_peer(row["peer_id"])
        peer_type = row["peer_type"]
        if peer_type not in {"user", "chat", "channel"}:
            peer_type = inferred_type
        # Replies are never separate threads in a private dialog.
        thread_id = 0 if peer_type == "user" else int(row["thread_id"] or 0)
        groups.setdefault((peer_type, canonical_peer, thread_id), []).append(row)

    for (peer_type, peer_id, thread_id), members in groups.items():
        preferred = [row for row in members if (
            str(row["peer_id"]) == peer_id and int(row["thread_id"] or 0) == thread_id
        )]
        canonical = max(preferred or members, key=lambda row: (
            float(row["activated_at"] or 0), int(row["trigger_id"] or 0), row["id"]
        ))
        member_ids = [row["id"] for row in members]
        newest = max(members, key=lambda row: (
            int(row["trigger_id"] or 0), float(row["activated_at"] or 0)
        ))
        latest = max(max(int(row["latest_relevant_message_id"] or 0),
                         int(row["trigger_id"] or 0)) for row in members)
        last_seen = min(latest, max(int(row["last_seen_message_id"] or 0)
                                    for row in members))

        group_notifications = [item for member_id in member_ids
                               for item in notifications_by_conversation.get(member_id, ())]
        notification_by_trigger: dict[int, list] = {}
        for item in group_notifications:
            notification_by_trigger.setdefault(int(item["trigger_id"]), []).append(item)
        retained_notifications = []
        for duplicates in notification_by_trigger.values():
            keep = min(duplicates, key=lambda item: int(item["id"]))
            payload = max(duplicates, key=lambda item: int(item["id"]))
            for duplicate in duplicates:
                if duplicate["id"] != keep["id"]:
                    connection.execute(text(
                        "DELETE FROM inbox_notifications WHERE id = :id"
                    ), {"id": duplicate["id"]})
            connection.execute(text(
                "UPDATE inbox_notifications SET peer_id = :peer_id, "
                "conversation_id = :conversation_id, title = :title, topic_title = :topic, "
                "preview = :preview, trigger_reason = :reason, unread_count = :unread, "
                "suppressed = :suppressed WHERE id = :id"
            ), {
                "peer_id": peer_id, "conversation_id": canonical["id"],
                "title": payload["title"], "topic": payload["topic_title"],
                "preview": payload["preview"], "reason": payload["trigger_reason"],
                "unread": max(int(item["unread_count"] or 0) for item in duplicates),
                "suppressed": int(all(bool(item["suppressed"]) for item in duplicates)),
                "id": keep["id"],
            })
            retained_notifications.append(keep)

        # A duplicate projection had its own local counter, so copying those
        # counters produces a regressing grouped banner after merge. Rebase the
        # retained cursor stream against the merged seen watermark instead.
        running_unread = 0
        for item in sorted(retained_notifications, key=lambda value: int(value["id"])):
            if int(item["trigger_id"]) > last_seen:
                running_unread += 1
            connection.execute(text(
                "UPDATE inbox_notifications SET unread_count = :unread WHERE id = :id"
            ), {"unread": running_unread, "id": item["id"]})

        distinct_unseen = {
            int(item["trigger_id"]) for item in group_notifications
            if int(item["trigger_id"]) > last_seen
        }
        if latest <= last_seen:
            unread = 0
        elif distinct_unseen:
            unread = len(distinct_unseen)
        else:
            unread = max(1, max(int(row["unread_count"] or 0) for row in members))

        pending = any(row["opened_at"] is None and not bool(row["manually_closed"])
                      for row in members)
        unclosed_opened = [row for row in members
                           if row["opened_at"] is not None
                           and not bool(row["manually_closed"])]
        all_closed = all(bool(row["manually_closed"]) for row in members)
        if pending:
            opened_at, expires_at, manually_closed = None, 0.0, False
        elif unclosed_opened:
            opened_at = min(float(row["opened_at"]) for row in unclosed_opened)
            expires_at = max(float(row["expires_at"] or 0) for row in unclosed_opened)
            manually_closed = False
        else:
            opened = [float(row["opened_at"]) for row in members
                      if row["opened_at"] is not None]
            opened_at = max(opened) if opened else None
            expires_at = max(float(row["expires_at"] or 0) for row in members)
            manually_closed = all_closed
            if all_closed:
                last_seen, unread = latest, 0

        access_hash = next((str(row["access_hash"]) for row in sorted(
            members, key=lambda row: float(row["activated_at"] or 0), reverse=True
        ) if row["access_hash"] is not None), None)
        library_source_id = next((row["library_source_id"] for row in members
                                  if row["library_source_id"]), None)
        live_rows = [row for row in members if row["quarantined_at"] is None]
        if live_rows:
            quarantined_at = quarantine_reason = None
        else:
            quarantined = max(members, key=lambda row: float(row["quarantined_at"] or 0))
            quarantined_at = quarantined["quarantined_at"]
            quarantine_reason = quarantined["quarantine_reason"]

        for duplicate in members:
            if duplicate["id"] == canonical["id"]:
                continue
            connection.execute(text(
                "UPDATE inbox_sends SET conversation_id = :canonical "
                "WHERE conversation_id = :duplicate"
            ), {"canonical": canonical["id"], "duplicate": duplicate["id"]})
            connection.execute(text(
                "DELETE FROM inbox_conversations WHERE id = :duplicate"
            ), {"duplicate": duplicate["id"]})

        connection.execute(text(
            "UPDATE inbox_conversations SET peer_type = :peer_type, peer_id = :peer_id, "
            "access_hash = :access_hash, thread_id = :thread_id, is_forum = :is_forum, "
            "title = :title, topic_title = :topic_title, preview = :preview, "
            "trigger_id = :trigger_id, activated_at = :activated_at, opened_at = :opened_at, "
            "latest_relevant_message_id = :latest, last_seen_message_id = :last_seen, "
            "unread_count = :unread, expires_at = :expires_at, "
            "manually_closed = :manually_closed, library_source_id = :library_source_id, "
            "quarantined_at = :quarantined_at, quarantine_reason = :quarantine_reason "
            "WHERE id = :id"
        ), {
            "peer_type": peer_type, "peer_id": peer_id, "access_hash": access_hash,
            "thread_id": thread_id, "is_forum": int(bool(newest["is_forum"])),
            "title": newest["title"], "topic_title": newest["topic_title"],
            "preview": newest["preview"], "trigger_id": newest["trigger_id"],
            "activated_at": newest["activated_at"], "opened_at": opened_at,
            "latest": latest, "last_seen": last_seen, "unread": unread,
            "expires_at": expires_at, "manually_closed": int(manually_closed),
            "library_source_id": library_source_id, "quarantined_at": quarantined_at,
            "quarantine_reason": quarantine_reason, "id": canonical["id"],
        })


def make_engine(settings: Settings):
    connect_args = (
        {"check_same_thread": False}
        if settings.database_url.startswith("sqlite")
        else {}
    )
    return create_engine(
        settings.database_url,
        future=True,
        connect_args=connect_args,
        hide_parameters=True,
    )


def init_db(settings: Settings) -> sessionmaker[Session]:
    engine = make_engine(settings)
    Base.metadata.create_all(engine)
    if engine.dialect.name == "sqlite":
        with engine.begin() as connection:
            inbox_columns = {row[1] for row in connection.execute(
                text("PRAGMA table_info(inbox_conversations)")
            )}
            if "opened_at" not in inbox_columns:
                connection.execute(text(
                    "ALTER TABLE inbox_conversations ADD COLUMN opened_at FLOAT"
                ))
                # Existing records keep their deadline; do not resurrect expired attention.
                connection.execute(text(
                    "UPDATE inbox_conversations SET opened_at = activated_at"
                ))
            read_columns = {
                "latest_relevant_message_id", "last_seen_message_id", "unread_count"
            }
            missing_read_columns = read_columns - inbox_columns
            for name in read_columns:
                if name not in inbox_columns:
                    connection.execute(text(
                        f"ALTER TABLE inbox_conversations ADD COLUMN {name} INTEGER DEFAULT 0"
                    ))
            inbox_additions = {
                "peer_type": "VARCHAR(16) DEFAULT 'unknown'",
                "access_hash": "VARCHAR(32)",
                "library_source_id": "VARCHAR(32)",
                "quarantined_at": "FLOAT",
                "quarantine_reason": "VARCHAR(32)",
            }
            for name, ddl in inbox_additions.items():
                if name not in inbox_columns:
                    connection.execute(text(
                        f"ALTER TABLE inbox_conversations ADD COLUMN {name} {ddl}"
                    ))
            if "latest_relevant_message_id" in missing_read_columns:
                connection.execute(text(
                    "UPDATE inbox_conversations SET latest_relevant_message_id = trigger_id"
                ))
            if "last_seen_message_id" in missing_read_columns:
                connection.execute(text(
                    "UPDATE inbox_conversations SET last_seen_message_id = CASE "
                    "WHEN opened_at IS NOT NULL OR manually_closed = 1 THEN trigger_id ELSE 0 END"
                ))
            if "unread_count" in missing_read_columns:
                connection.execute(text(
                    "UPDATE inbox_conversations SET unread_count = CASE "
                    "WHEN opened_at IS NULL AND manually_closed = 0 THEN 1 ELSE 0 END"
                ))
            notification_columns = {row[1] for row in connection.execute(
                text("PRAGMA table_info(inbox_notifications)")
            )}
            if "unread_count" not in notification_columns:
                connection.execute(text(
                    "ALTER TABLE inbox_notifications ADD COLUMN unread_count INTEGER DEFAULT 1"
                ))
            if "suppressed" not in notification_columns:
                connection.execute(text(
                    "ALTER TABLE inbox_notifications ADD COLUMN suppressed BOOLEAN DEFAULT 0"
                ))
            # The handoff build already contained the three read-state columns,
            # but left existing rows at their all-zero server defaults.  That is
            # distinguishable from the durable format because a non-zero trigger
            # must also be reflected in ``latest_relevant_message_id``.  Repair
            # those rows before duplicate identities are folded together.
            connection.execute(text(
                "UPDATE inbox_conversations SET "
                "latest_relevant_message_id = trigger_id, "
                "last_seen_message_id = CASE "
                "WHEN opened_at IS NOT NULL OR manually_closed = 1 "
                "THEN trigger_id ELSE 0 END, "
                "unread_count = CASE "
                "WHEN opened_at IS NOT NULL OR manually_closed = 1 THEN 0 "
                "ELSE MAX(1, (SELECT COUNT(DISTINCT n.trigger_id) "
                "FROM inbox_notifications AS n "
                "WHERE n.conversation_id = inbox_conversations.id)) END "
                "WHERE trigger_id > 0 "
                "AND COALESCE(latest_relevant_message_id, 0) = 0 "
                "AND COALESCE(last_seen_message_id, 0) = 0 "
                "AND COALESCE(unread_count, 0) = 0"
            ))
            _merge_inbox_duplicates(connection)
            connection.execute(text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_inbox_peer_type_thread "
                "ON inbox_conversations (peer_type, peer_id, thread_id)"
            ))
            connection.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_inbox_conversations_library_source_id "
                "ON inbox_conversations (library_source_id)"
            ))
            connection.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_inbox_conversations_quarantined_at "
                "ON inbox_conversations (quarantined_at)"
            ))
            columns = {
                row[1]
                for row in connection.execute(text("PRAGMA table_info(messages)")).fetchall()
            }
            if "digested_at" not in columns:
                connection.execute(text("ALTER TABLE messages ADD COLUMN digested_at DATETIME"))
            if "raw_redacted_at" not in columns:
                connection.execute(text("ALTER TABLE messages ADD COLUMN raw_redacted_at DATETIME"))
            if "is_backfilled" not in columns:
                connection.execute(
                    text("ALTER TABLE messages ADD COLUMN is_backfilled BOOLEAN DEFAULT 0")
                )
            if "ingested_at" not in columns:
                connection.execute(text("ALTER TABLE messages ADD COLUMN ingested_at DATETIME"))
            if "is_outgoing" not in columns:
                connection.execute(text("ALTER TABLE messages ADD COLUMN is_outgoing BOOLEAN"))
            if "p0_classified_at" not in columns:
                connection.execute(
                    text("ALTER TABLE messages ADD COLUMN p0_classified_at DATETIME")
                )
            if "p0_classification" not in columns:
                connection.execute(
                    text("ALTER TABLE messages ADD COLUMN p0_classification VARCHAR(32)")
                )
            if "p0_llm_called_at" not in columns:
                connection.execute(
                    text("ALTER TABLE messages ADD COLUMN p0_llm_called_at DATETIME")
                )
            if "p0_confidence" not in columns:
                connection.execute(text("ALTER TABLE messages ADD COLUMN p0_confidence FLOAT"))
            if "claimed_digest_id" not in columns:
                connection.execute(
                    text("ALTER TABLE messages ADD COLUMN claimed_digest_id INTEGER")
                )
                connection.execute(
                    text("CREATE INDEX IF NOT EXISTS ix_messages_claimed_digest_id "
                         "ON messages (claimed_digest_id)")
                )
            if "reply_to_is_mine" not in columns:
                connection.execute(
                    text("ALTER TABLE messages ADD COLUMN reply_to_is_mine BOOLEAN")
                )
            library_columns = {row[1] for row in connection.execute(
                text("PRAGMA table_info(library_sources)")
            )}
            library_additions = {
                "manual_write_enabled": "BOOLEAN DEFAULT 0",
                "manual_open_date": "DATE",
                "manual_access_until": "FLOAT DEFAULT 0",
            }
            for name, ddl in library_additions.items():
                if name not in library_columns:
                    connection.execute(text(
                        f"ALTER TABLE library_sources ADD COLUMN {name} {ddl}"
                    ))
            connection.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_library_sources_manual_write_enabled "
                "ON library_sources (manual_write_enabled)"
            ))
            digest_columns = {
                row[1]
                for row in connection.execute(text("PRAGMA table_info(digests)")).fetchall()
            }
            if "digest_key" not in digest_columns:
                connection.execute(text("ALTER TABLE digests ADD COLUMN digest_key VARCHAR(256)"))
                connection.execute(
                    text("CREATE UNIQUE INDEX IF NOT EXISTS uq_digest_key ON digests (digest_key)")
                )
            if "delivery_id" not in digest_columns:
                connection.execute(text("ALTER TABLE digests ADD COLUMN delivery_id VARCHAR(256)"))
            if "source_chat_ids" not in digest_columns:
                connection.execute(text("ALTER TABLE digests ADD COLUMN source_chat_ids TEXT"))
            birthday_notification_columns = {
                row[1]
                for row in connection.execute(
                    text("PRAGMA table_info(birthday_notifications)")
                ).fetchall()
            }
            if "attempted_at" not in birthday_notification_columns:
                connection.execute(
                    text("ALTER TABLE birthday_notifications ADD COLUMN attempted_at DATETIME")
                )
    return sessionmaker(engine, expire_on_commit=False, future=True)


def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    with factory() as session:
        yield session
