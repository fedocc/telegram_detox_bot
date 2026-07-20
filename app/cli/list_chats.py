from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Callable

from app.config import Settings, get_settings
from app.ignored_chats import load_ignored_chats_from_settings
from app.services.time_format import format_user_datetime
from app.telegram.client import make_client
from app.telegram.mapper import display_name


def _dialog_type(dialog) -> str:
    if getattr(dialog, "is_user", False):
        return "private"
    if getattr(dialog, "is_group", False):
        return "group"
    if getattr(dialog, "is_channel", False):
        return "channel"
    return "group"


def _positive_limit(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("limit must be at least 1")
    return parsed


async def run(
    settings: Settings,
    *,
    limit: int = 100,
    search: str | None = None,
    output: Callable[[str], None] = print,
) -> int:
    ignored = load_ignored_chats_from_settings(settings)
    client = make_client(settings)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise RuntimeError("Telegram session is unauthorized. Run telegram_login.")
        normalized_search = (search or "").strip().casefold()
        listed = 0
        async for dialog in client.iter_dialogs():
            entity = dialog.entity
            title = getattr(dialog, "name", None) or display_name(entity)
            username = getattr(entity, "username", None)
            searchable = f"{title} {username or ''}".casefold()
            if normalized_search and normalized_search not in searchable:
                continue
            last_seen = getattr(dialog, "date", None)
            output(
                json.dumps(
                    {
                        "chat_id": str(dialog.id),
                        "type": _dialog_type(dialog),
                        "title": title,
                        "username": username,
                        "last_seen": format_user_datetime(last_seen) if last_seen else None,
                        "ignored": ignored.contains(dialog.id),
                    },
                    ensure_ascii=False,
                )
            )
            listed += 1
            if listed >= limit:
                break
        return listed
    finally:
        await client.disconnect()


def main() -> None:
    parser = argparse.ArgumentParser(description="List Telegram chats without changing them")
    parser.add_argument("--limit", type=_positive_limit, default=100)
    parser.add_argument("--search")
    args = parser.parse_args()
    asyncio.run(
        run(
            get_settings(),
            limit=args.limit,
            search=args.search,
        )
    )


if __name__ == "__main__":
    main()
