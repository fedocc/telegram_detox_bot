from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta
from pathlib import Path

from sqlalchemy.orm import Session

from app.birthdays.manual import load_manual_birthdays
from app.birthdays.models import (
    BirthdayPerson,
    BirthdayPollStats,
    BirthdaySourceRecord,
    DueBirthday,
)
from app.birthdays.telegram import fetch_telegram_birthdays
from app.config import Settings
from app.db import repository
from app.email.sender import EmailSender

logger = logging.getLogger(__name__)


def _store_records(
    session: Session,
    records: list[BirthdaySourceRecord],
    seen_at: datetime,
) -> None:
    for record in records:
        repository.upsert_birthday_contact(
            session,
            person_key=record.person_key,
            telegram_user_id=record.telegram_user_id,
            display_name_safe=record.display_name,
            username=record.username,
            day=record.day,
            month=record.month,
            year=record.year,
            source=record.source,
            seen_at=seen_at,
        )


def sync_manual_birthdays(session: Session, path: Path, now: datetime) -> int:
    records = load_manual_birthdays(path)
    _store_records(session, records, now)
    repository.prune_manual_birthday_contacts(
        session,
        {record.person_key for record in records},
    )
    return len(records)


async def sync_telegram_birthdays(session: Session, client, now: datetime) -> int:
    records = await fetch_telegram_birthdays(client)
    _store_records(session, records, now)
    return len(records)


def _normalized_name(value: str) -> str:
    return " ".join(value.casefold().split())


def _same_person(person: BirthdayPerson, record) -> bool:
    if person.username and record.username:
        if person.username.casefold() == record.username.casefold():
            return True
    return bool(
        _normalized_name(person.display_name) == _normalized_name(record.display_name_safe)
        and person.day == record.day
        and person.month == record.month
    )


def merged_birthday_people(session: Session) -> list[BirthdayPerson]:
    rows = sorted(
        repository.birthday_contacts(session),
        key=lambda row: (row.source != "telegram", row.id),
    )
    people: list[BirthdayPerson] = []
    for row in rows:
        person = next((candidate for candidate in people if _same_person(candidate, row)), None)
        if person is None:
            people.append(
                BirthdayPerson(
                    person_key=row.person_key,
                    aliases={row.person_key},
                    display_name=row.display_name_safe,
                    username=row.username,
                    day=row.day,
                    month=row.month,
                    year=row.year,
                    sources={row.source},
                )
            )
            continue
        person.aliases.add(row.person_key)
        person.sources.add(row.source)
        if row.source == "telegram":
            person.person_key = row.person_key
        if row.source == "manual":
            person.display_name = row.display_name_safe
            person.username = row.username or person.username
            person.day = row.day
            person.month = row.month
            person.year = row.year
    return people


def _birthday_matches(person: BirthdayPerson, target: date) -> bool:
    if person.month == 2 and person.day == 29:
        try:
            date(target.year, 2, 29)
        except ValueError:
            return target.month == 2 and target.day == 28
    return target.month == person.month and target.day == person.day


def due_birthdays(
    session: Session,
    today: date,
    lookahead_days: int,
) -> list[DueBirthday]:
    due: list[DueBirthday] = []
    for offset in range(min(lookahead_days, 1) + 1):
        target = today + timedelta(days=offset)
        notification_type = "today" if offset == 0 else "tomorrow"
        for person in merged_birthday_people(session):
            if not _birthday_matches(person, target):
                continue
            if repository.has_birthday_notification(
                session,
                person.aliases,
                target,
                notification_type,
            ):
                continue
            due.append(
                DueBirthday(
                    person=person,
                    birthday_date=target,
                    notification_type=notification_type,
                )
            )
    return due


def _source_label(sources: set[str]) -> str:
    if sources == {"telegram", "manual"}:
        return "Telegram / manual"
    if "telegram" in sources:
        return "Telegram"
    return "manual"


def render_birthday_email(due: list[DueBirthday]) -> tuple[str, str]:
    today = [item for item in due if item.notification_type == "today"]
    tomorrow = [item for item in due if item.notification_type == "tomorrow"]
    if today:
        subject = "[ДР] Сегодня: " + ", ".join(item.person.display_name for item in today)
    else:
        subject = "[ДР] Скоро дни рождения"

    def lines(items: list[DueBirthday], label: str) -> list[str]:
        return [f"- {item.person.display_name} — {label}" for item in items] or ["- Нет"]

    source_labels = sorted({_source_label(item.person.sources) for item in due})
    body_lines = [
        "Сегодня:",
        *lines(today, "сегодня"),
        "",
        "Завтра:",
        *lines(tomorrow, "завтра"),
        "",
        "Источник:",
        *(f"- {source}" for source in source_labels),
        "",
        "Что сделать:",
        "- поздравить / написать в Telegram",
    ]
    return subject, "\n".join(body_lines)


def send_birthday_notifications(
    session: Session,
    email_sender: EmailSender,
    now: datetime,
    lookahead_days: int,
) -> int:
    repository.release_stale_birthday_notification_claims(session, now)
    due = due_birthdays(session, now.date(), lookahead_days)
    claimed: list[DueBirthday] = []
    for item in due:
        if repository.claim_birthday_notification(
            session,
            person_key=item.person.person_key,
            birthday_date=item.birthday_date,
            notification_type=item.notification_type,
            claimed_at=now,
        ):
            claimed.append(item)
    if not claimed:
        return 0

    subject, body = render_birthday_email(claimed)
    try:
        email_sender.send(subject, body)
    except Exception:
        for item in claimed:
            repository.release_birthday_notification(
                session,
                person_key=item.person.person_key,
                birthday_date=item.birthday_date,
                notification_type=item.notification_type,
            )
        raise
    for item in claimed:
        repository.mark_birthday_notification_sent(
            session,
            person_key=item.person.person_key,
            birthday_date=item.birthday_date,
            notification_type=item.notification_type,
            sent_at=now,
        )
    today_count = sum(item.notification_type == "today" for item in claimed)
    logger.info(
        "Birthday email sent notifications=%s today=%s tomorrow=%s",
        len(claimed),
        today_count,
        len(claimed) - today_count,
    )
    return len(claimed)


def _configured_time(value: str) -> time:
    hour, minute = (int(part) for part in value.split(":", 1))
    return time(hour, minute)


async def poll_birthdays_once(
    session: Session,
    client,
    email_sender: EmailSender,
    settings: Settings,
    now: datetime,
) -> BirthdayPollStats:
    telegram_count = await sync_telegram_birthdays(session, client, now)
    manual_count = sync_manual_birthdays(session, settings.birthday_manual_path, now)
    notifications_sent = 0
    if now.timetz().replace(tzinfo=None) >= _configured_time(settings.birthday_reminder_time):
        notifications_sent = send_birthday_notifications(
            session,
            email_sender,
            now,
            lookahead_days=0,
        )
    logger.info(
        "Birthday poll completed telegram_contacts=%s manual_contacts=%s notifications=%s",
        telegram_count,
        manual_count,
        notifications_sent,
    )
    return BirthdayPollStats(telegram_count, manual_count, notifications_sent)


def run_daily_birthday_reminders(
    session: Session,
    email_sender: EmailSender,
    settings: Settings,
    now: datetime,
) -> int:
    manual_count = sync_manual_birthdays(session, settings.birthday_manual_path, now)
    sent = send_birthday_notifications(
        session,
        email_sender,
        now,
        lookahead_days=settings.birthday_lookahead_days,
    )
    logger.info(
        "Birthday daily check completed manual_contacts=%s notifications=%s",
        manual_count,
        sent,
    )
    return sent
