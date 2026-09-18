from __future__ import annotations

import sqlite3

import pytest
from sqlalchemy import text

from app.db.session import init_db
from app.inbox.store import InboxStore

CANONICAL_ID = "a" * 32
DUPLICATE_ID = "b" * 32
CHANNEL_ID = "d" * 32


# Frozen, relevant subset of the SQLite schema at 465a6f7.  Keep this as SQL:
# constructing the database through today's ORM would make this regression test
# incapable of detecting a broken upgrade from a real on-disk installation.
SCHEMA_465A6F7 = """
CREATE TABLE messages (
    id INTEGER NOT NULL PRIMARY KEY,
    chat_id VARCHAR(128) NOT NULL,
    chat_title VARCHAR(512) NOT NULL,
    chat_type VARCHAR(32) NOT NULL,
    sender_id VARCHAR(128),
    sender_name VARCHAR(512),
    message_id INTEGER NOT NULL,
    timestamp DATETIME NOT NULL,
    is_outgoing BOOLEAN,
    reply_to_message_id INTEGER,
    reply_to_is_mine BOOLEAN,
    text TEXT,
    media_type VARCHAR(32) NOT NULL DEFAULT 'none',
    caption TEXT,
    alert_sent BOOLEAN NOT NULL DEFAULT 0,
    is_backfilled BOOLEAN NOT NULL DEFAULT 0,
    ingested_at DATETIME,
    p0_review_candidate BOOLEAN NOT NULL DEFAULT 0,
    digested_at DATETIME,
    raw_redacted_at DATETIME,
    p0_classified_at DATETIME,
    p0_llm_called_at DATETIME,
    p0_classification VARCHAR(32),
    p0_confidence FLOAT,
    claimed_digest_id INTEGER,
    CONSTRAINT uq_chat_message UNIQUE (chat_id, message_id)
);
CREATE TABLE birthday_contacts (
    id INTEGER NOT NULL PRIMARY KEY,
    person_key VARCHAR(128) NOT NULL,
    telegram_user_id BIGINT,
    display_name_safe VARCHAR(512) NOT NULL,
    username VARCHAR(128),
    day INTEGER NOT NULL,
    month INTEGER NOT NULL,
    year INTEGER,
    source VARCHAR(32) NOT NULL,
    first_seen_at DATETIME NOT NULL,
    last_seen_at DATETIME NOT NULL,
    CONSTRAINT uq_birthday_contact_person UNIQUE (person_key)
);
CREATE TABLE birthday_notifications (
    id INTEGER NOT NULL PRIMARY KEY,
    person_key VARCHAR(128) NOT NULL,
    birthday_date DATE NOT NULL,
    notification_type VARCHAR(16) NOT NULL,
    sent_at DATETIME,
    claimed_at DATETIME,
    attempted_at DATETIME,
    CONSTRAINT uq_birthday_notification_person_date_type
        UNIQUE (person_key, birthday_date, notification_type)
);
CREATE TABLE inbox_conversations (
    id VARCHAR(32) NOT NULL PRIMARY KEY,
    peer_id VARCHAR(128) NOT NULL,
    thread_id INTEGER NOT NULL DEFAULT 0,
    is_forum BOOLEAN NOT NULL DEFAULT 0,
    title VARCHAR(512) NOT NULL,
    topic_title VARCHAR(512) NOT NULL DEFAULT '',
    preview VARCHAR(256) NOT NULL DEFAULT '',
    trigger_id INTEGER NOT NULL,
    activated_at FLOAT NOT NULL,
    opened_at FLOAT,
    expires_at FLOAT NOT NULL,
    manually_closed BOOLEAN NOT NULL DEFAULT 0,
    CONSTRAINT uq_inbox_peer_thread UNIQUE (peer_id, thread_id)
);
CREATE INDEX ix_inbox_conversations_expires_at
    ON inbox_conversations (expires_at);
CREATE TABLE inbox_sends (
    request_id VARCHAR(36) NOT NULL PRIMARY KEY,
    conversation_id VARCHAR(32) NOT NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'pending',
    message_id INTEGER,
    created_at FLOAT NOT NULL
);
CREATE TABLE inbox_notifications (
    id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
    peer_id VARCHAR(128) NOT NULL,
    trigger_id INTEGER NOT NULL,
    conversation_id VARCHAR(32) NOT NULL,
    title VARCHAR(160) NOT NULL,
    topic_title VARCHAR(160) NOT NULL DEFAULT '',
    preview VARCHAR(240) NOT NULL,
    trigger_reason VARCHAR(32) NOT NULL,
    created_at FLOAT NOT NULL,
    CONSTRAINT uq_inbox_notification_trigger UNIQUE (peer_id, trigger_id)
);
CREATE INDEX ix_inbox_notifications_conversation_id
    ON inbox_notifications (conversation_id);
CREATE INDEX ix_inbox_notifications_created_at
    ON inbox_notifications (created_at);
"""


def build_frozen_database(path, variant):
    connection = sqlite3.connect(path)
    try:
        connection.executescript(SCHEMA_465A6F7)
        if variant == "handoff_zero_sentinel":
            connection.executescript("""
                ALTER TABLE inbox_conversations
                    ADD COLUMN latest_relevant_message_id INTEGER DEFAULT 0;
                ALTER TABLE inbox_conversations
                    ADD COLUMN last_seen_message_id INTEGER DEFAULT 0;
                ALTER TABLE inbox_conversations
                    ADD COLUMN unread_count INTEGER DEFAULT 0;
                ALTER TABLE inbox_notifications
                    ADD COLUMN unread_count INTEGER DEFAULT 1;
            """)

        connection.execute("""
            INSERT INTO messages (
                id, chat_id, chat_title, chat_type, sender_id, sender_name,
                message_id, timestamp, is_outgoing, reply_to_message_id,
                reply_to_is_mine, text, media_type, caption, alert_sent,
                is_backfilled, ingested_at, p0_review_candidate, digested_at,
                raw_redacted_at, p0_classified_at, p0_llm_called_at,
                p0_classification, p0_confidence, claimed_digest_id
            ) VALUES (
                7, '-1007', 'Durable chat', 'channel', '77', 'Sender',
                700, '2026-07-07 09:00:00', 0, NULL, NULL,
                'durable message', 'none', NULL, 1, 0,
                '2026-07-07 09:00:01', 0, NULL, NULL, NULL, NULL, NULL, NULL, NULL
            )
        """)
        connection.execute("""
            INSERT INTO birthday_contacts (
                id, person_key, telegram_user_id, display_name_safe, username,
                day, month, year, source, first_seen_at, last_seen_at
            ) VALUES (
                3, 'telegram:77', 77, 'Birthday Person', 'birthday_person',
                8, 9, 1990, 'telegram',
                '2026-07-01 10:00:00', '2026-07-07 10:00:00'
            )
        """)
        connection.execute("""
            INSERT INTO birthday_notifications (
                id, person_key, birthday_date, notification_type,
                sent_at, claimed_at, attempted_at
            ) VALUES (
                4, 'telegram:77', '2026-09-08', 'day',
                '2026-09-08 08:00:00', NULL, '2026-09-08 08:00:00'
            )
        """)

        conversations = (
            (
                CANONICAL_ID, "42", 0, 0, "Opened private", "", "read preview",
                10, 1000.0, 1000.0, 1300.0, 0,
            ),
            (
                DUPLICATE_ID, "042", 77, 0, "Latest private", "", "unread preview",
                12, 1100.0, None, 0.0, 0,
            ),
            (
                CHANNEL_ID, "-1000000000123", 55, 1, "Opened channel", "Topic",
                "channel preview", 50, 1050.0, 1050.0, 1350.0, 0,
            ),
        )
        connection.executemany("""
            INSERT INTO inbox_conversations (
                id, peer_id, thread_id, is_forum, title, topic_title, preview,
                trigger_id, activated_at, opened_at, expires_at, manually_closed
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, conversations)
        connection.executemany("""
            INSERT INTO inbox_sends (
                request_id, conversation_id, status, message_id, created_at
            ) VALUES (?, ?, ?, ?, ?)
        """, (
            ("00000000-0000-0000-0000-000000000001", DUPLICATE_ID,
             "pending", None, 1110.0),
            ("00000000-0000-0000-0000-000000000002", CHANNEL_ID,
             "sent", 500, 1060.0),
        ))
        connection.executemany("""
            INSERT INTO inbox_notifications (
                id, peer_id, trigger_id, conversation_id, title, topic_title,
                preview, trigger_reason, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            (101, "42", 10, CANONICAL_ID, "Opened private", "", "read",
             "private_message", 1000.0),
            (102, "042", 11, DUPLICATE_ID, "Latest private", "", "first unread",
             "private_message", 1090.0),
            (103, "042", 12, DUPLICATE_ID, "Old duplicate", "", "old payload",
             "private_message", 1095.0),
            (104, "42", 12, CANONICAL_ID, "Latest private", "", "new payload",
             "private_message", 1100.0),
            (110, "-1000000000123", 50, CHANNEL_ID, "Opened channel", "Topic",
             "already read", "mention", 1050.0),
        ))
        connection.commit()
    finally:
        connection.close()


@pytest.mark.parametrize(
    "legacy_variant", ("commit_465a6f7", "handoff_zero_sentinel"),
)
def test_frozen_inbox_schema_migrates_losslessly_and_idempotently(
        settings, tmp_path, legacy_variant):
    database = tmp_path / f"{legacy_variant}.db"
    build_frozen_database(database, legacy_variant)
    migrated_settings = settings.model_copy(update={
        "database_url": f"sqlite:///{database}",
    })

    first = init_db(migrated_settings)
    first.kw["bind"].dispose()
    factory = init_db(migrated_settings)

    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute("""
            SELECT * FROM inbox_conversations ORDER BY id
        """).fetchall()
        assert [row["id"] for row in rows] == [CANONICAL_ID, CHANNEL_ID]

        private = rows[0]
        assert (private["peer_type"], private["peer_id"], private["thread_id"]) == (
            "user", "42", 0,
        )
        assert private["title"] == "Latest private"
        assert private["preview"] == "unread preview"
        assert private["trigger_id"] == private["latest_relevant_message_id"] == 12
        assert private["last_seen_message_id"] == 10
        assert private["unread_count"] == 2
        assert private["opened_at"] is None
        assert private["expires_at"] == 0
        assert private["manually_closed"] == 0

        channel = rows[1]
        assert (channel["peer_type"], channel["peer_id"], channel["thread_id"]) == (
            "channel", "-1000000000123", 55,
        )
        assert channel["opened_at"] == 1050.0
        assert channel["expires_at"] == 1350.0
        assert channel["latest_relevant_message_id"] == 50
        assert channel["last_seen_message_id"] == 50
        assert channel["unread_count"] == 0

        notifications = connection.execute("""
            SELECT id, peer_id, trigger_id, conversation_id, preview,
                   unread_count, suppressed
            FROM inbox_notifications ORDER BY id
        """).fetchall()
        assert [row["id"] for row in notifications] == [101, 102, 103, 110]
        private_notifications = notifications[:3]
        assert all(row["peer_id"] == "42" for row in private_notifications)
        assert all(row["conversation_id"] == CANONICAL_ID
                   for row in private_notifications)
        assert [row["trigger_id"] for row in private_notifications] == [10, 11, 12]
        assert [row["unread_count"] for row in private_notifications] == [0, 1, 2]
        assert private_notifications[-1]["preview"] == "new payload"
        assert all(row["suppressed"] == 0 for row in notifications)

        sends = connection.execute("""
            SELECT request_id, conversation_id, status, message_id
            FROM inbox_sends ORDER BY request_id
        """).fetchall()
        assert [row["conversation_id"] for row in sends] == [
            CANONICAL_ID, CHANNEL_ID,
        ]
        assert [(row["status"], row["message_id"]) for row in sends] == [
            ("pending", None), ("sent", 500),
        ]

        assert dict(connection.execute("""
            SELECT person_key, display_name_safe, day, month, year
            FROM birthday_contacts WHERE id = 3
        """).fetchone()) == {
            "person_key": "telegram:77", "display_name_safe": "Birthday Person",
            "day": 8, "month": 9, "year": 1990,
        }
        assert dict(connection.execute("""
            SELECT person_key, birthday_date, notification_type, attempted_at
            FROM birthday_notifications WHERE id = 4
        """).fetchone()) == {
            "person_key": "telegram:77", "birthday_date": "2026-09-08",
            "notification_type": "day", "attempted_at": "2026-09-08 08:00:00",
        }
        assert dict(connection.execute("""
            SELECT chat_id, message_id, text, alert_sent FROM messages WHERE id = 7
        """).fetchone()) == {
            "chat_id": "-1007", "message_id": 700,
            "text": "durable message", "alert_sent": 1,
        }
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        connection.close()

    store = InboxStore(factory, lambda: set(), clock=lambda: 1200.0)
    feed = store.notifications(0)
    assert feed["cursor"] == 110
    assert [(event["event_id"], event["conversation_id"], event["unread_count"])
            for event in feed["events"]] == [
        (102, CANONICAL_ID, 1), (103, CANONICAL_ID, 2),
    ]

    with factory() as session:
        next_id = session.execute(text("""
            INSERT INTO inbox_notifications (
                peer_id, trigger_id, conversation_id, title, topic_title,
                preview, trigger_reason, unread_count, suppressed, created_at
            ) VALUES (
                '42', 13, :conversation_id, 'Latest private', '', 'future event',
                'private_message', 3, 0, 1200.0
            ) RETURNING id
        """), {"conversation_id": CANONICAL_ID}).scalar_one()
        session.commit()
    assert next_id > 110
    with factory.kw["bind"].connect() as migrated:
        assert migrated.exec_driver_sql("PRAGMA integrity_check").scalar_one() == "ok"
    factory.kw["bind"].dispose()
