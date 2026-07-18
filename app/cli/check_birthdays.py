from __future__ import annotations

import argparse
import asyncio
from collections.abc import Callable
from datetime import datetime
from zoneinfo import ZoneInfo

from app.birthdays.service import (
    due_birthdays,
    send_birthday_notifications,
    sync_manual_birthdays,
    sync_telegram_birthdays,
)
from app.config import Settings, get_settings
from app.db.session import init_db
from app.email.sender import EmailSender
from app.telegram.client import make_client

SAFE_SAMPLE = """Пример письма без реальных данных:
[ДР] Сегодня: Пример Контакта

Сегодня:
- Пример Контакта — сегодня

Источник:
- Telegram / manual

Что сделать:
- поздравить / написать в Telegram"""


async def run(
    settings: Settings,
    *,
    dry_run: bool,
    output: Callable[[str], None] = print,
) -> int:
    session_factory = init_db(settings)
    client = make_client(settings)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise RuntimeError("Telegram session is unauthorized. Run telegram_login.")
        now = datetime.now(ZoneInfo(settings.timezone))
        with session_factory() as session:
            telegram_count = await sync_telegram_birthdays(session, client, now)
            manual_count = sync_manual_birthdays(session, settings.birthday_manual_path, now)
            pending_count = len(
                due_birthdays(
                    session,
                    now.date(),
                    settings.birthday_lookahead_days,
                )
            )
            if dry_run:
                output(
                    "Dry-run: "
                    f"telegram_contacts={telegram_count} "
                    f"manual_contacts={manual_count} pending_notifications={pending_count}"
                )
                output(SAFE_SAMPLE)
                return 0
            sent = send_birthday_notifications(
                session,
                EmailSender(settings),
                now,
                settings.birthday_lookahead_days,
            )
            output(f"Birthday notifications sent: {sent}")
            return sent
    finally:
        await client.disconnect()


def main() -> None:
    parser = argparse.ArgumentParser(description="Check Telegram and manual birthdays")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    asyncio.run(run(get_settings(), dry_run=args.dry_run))


if __name__ == "__main__":
    main()
