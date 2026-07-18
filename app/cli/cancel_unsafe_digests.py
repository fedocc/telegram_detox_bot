from __future__ import annotations

from app.config import get_settings
from app.db import repository
from app.db.session import init_db
from app.ignored_chats import load_ignored_chats_from_settings


def main() -> None:
    settings = get_settings()
    ignored_chat_ids = load_ignored_chats_from_settings(settings).chat_ids
    session_factory = init_db(settings)
    with session_factory() as session:
        cancelled = repository.cancel_unsafe_pending_digests(session, ignored_chat_ids)
    print(f"Cancelled unsafe digest jobs: {cancelled}")


if __name__ == "__main__":
    main()
