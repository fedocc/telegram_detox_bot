from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date


@dataclass(frozen=True)
class BirthdaySourceRecord:
    person_key: str
    telegram_user_id: int | None
    display_name: str
    username: str | None
    day: int
    month: int
    year: int | None
    source: str


@dataclass
class BirthdayPerson:
    person_key: str
    aliases: set[str]
    display_name: str
    username: str | None
    day: int
    month: int
    year: int | None
    sources: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class DueBirthday:
    person: BirthdayPerson
    birthday_date: date
    notification_type: str


@dataclass(frozen=True)
class BirthdayPollStats:
    telegram_contacts: int
    manual_contacts: int
    notifications_sent: int

