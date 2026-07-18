from __future__ import annotations

from telethon.tl.functions.contacts import GetBirthdaysRequest

from app.birthdays.models import BirthdaySourceRecord


def _display_name(user, contact_id: int) -> str:
    name = " ".join(
        " ".join(part.split())
        for part in (getattr(user, "first_name", None), getattr(user, "last_name", None))
        if isinstance(part, str) and part.strip()
    )
    username = getattr(user, "username", None)
    if name:
        return name[:512]
    if isinstance(username, str) and username:
        return f"@{' '.join(username.split())}"[:512]
    return f"Контакт Telegram {contact_id}"


async def fetch_telegram_birthdays(client) -> list[BirthdaySourceRecord]:
    result = await client(GetBirthdaysRequest())
    users = {int(user.id): user for user in getattr(result, "users", [])}
    records: list[BirthdaySourceRecord] = []
    for contact in getattr(result, "contacts", []):
        contact_id = int(contact.contact_id)
        birthday = contact.birthday
        user = users.get(contact_id)
        username = getattr(user, "username", None)
        year = getattr(birthday, "year", None)
        records.append(
            BirthdaySourceRecord(
                person_key=f"telegram:{contact_id}",
                telegram_user_id=contact_id,
                display_name=_display_name(user, contact_id),
                username=username if isinstance(username, str) and username else None,
                day=int(birthday.day),
                month=int(birthday.month),
                year=int(year) if year is not None else None,
                source="telegram",
            )
        )
    return records
