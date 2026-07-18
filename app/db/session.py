from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.db.tables import Base


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
