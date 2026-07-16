from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.db.tables import Base


def make_engine(settings: Settings):
    connect_args = (
        {"check_same_thread": False}
        if settings.database_url.startswith("sqlite")
        else {}
    )
    return create_engine(settings.database_url, future=True, connect_args=connect_args)


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
            if "p0_classified_at" not in columns:
                connection.execute(text("ALTER TABLE messages ADD COLUMN p0_classified_at DATETIME"))
            if "p0_classification" not in columns:
                connection.execute(text("ALTER TABLE messages ADD COLUMN p0_classification VARCHAR(32)"))
    return sessionmaker(engine, expire_on_commit=False, future=True)


def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    with factory() as session:
        yield session
