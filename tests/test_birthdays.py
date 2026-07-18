from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from telethon.tl.functions.contacts import GetBirthdaysRequest

from app.birthdays.service import (
    birthday_message_id,
    due_birthdays,
    poll_birthdays_once,
    send_birthday_notifications,
    sync_manual_birthdays,
    sync_telegram_birthdays,
)
from app.cli import check_birthdays
from app.db import repository


class FakeBirthdayClient:
    def __init__(self, contacts: list, users: list) -> None:
        self.result = SimpleNamespace(contacts=contacts, users=users)
        self.requests: list[object] = []
        self.connected = False
        self.disconnected = False

    async def __call__(self, request):
        self.requests.append(request)
        return self.result

    async def connect(self) -> None:
        self.connected = True

    async def is_user_authorized(self) -> bool:
        return True

    async def disconnect(self) -> None:
        self.disconnected = True


class FakeEmail:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.message_ids: list[str | None] = []

    def send(
        self,
        subject: str,
        text: str,
        html: str | None = None,
        **kwargs,
    ) -> None:
        self.sent.append((subject, text))
        self.message_ids.append(kwargs.get("message_id"))


class AcceptedThenRaisesEmail(FakeEmail):
    def __init__(self) -> None:
        super().__init__()
        self.provider_accepts = 0

    def send(self, subject: str, text: str, html: str | None = None, **kwargs) -> None:
        self.provider_accepts += 1
        raise RuntimeError("email unavailable")


def birthday_contact(
    user_id: int,
    *,
    day: int,
    month: int,
    year: int | None = None,
):
    return SimpleNamespace(
        contact_id=user_id,
        birthday=SimpleNamespace(day=day, month=month, year=year),
    )


def birthday_user(
    user_id: int,
    *,
    first_name: str,
    last_name: str = "",
    username: str | None = None,
):
    return SimpleNamespace(
        id=user_id,
        first_name=first_name,
        last_name=last_name,
        username=username,
    )


async def test_telegram_birthday_today_sends_email(session, settings, tmp_path) -> None:
    settings.birthday_manual_path = tmp_path / "missing.json"
    now = datetime.fromisoformat("2026-07-18T12:00:00+03:00")
    client = FakeBirthdayClient(
        [birthday_contact(101, day=18, month=7, year=1998)],
        [birthday_user(101, first_name="Иван", last_name="Петров", username="ivan")],
    )
    email = FakeEmail()

    stats = await poll_birthdays_once(session, client, email, settings, now)

    assert isinstance(client.requests[0], GetBirthdaysRequest)
    assert stats.telegram_contacts == 1
    assert stats.notifications_sent == 1
    assert email.sent[0][0] == "[Telegram Detox][ДР] Сегодня: Иван Петров"
    assert "Иван Петров — сегодня" in email.sent[0][1]
    assert "- Telegram" in email.sent[0][1]
    assert "поздравить / написать в Telegram" in email.sent[0][1]


async def test_telegram_birthday_tomorrow_sends_email(session, settings, tmp_path) -> None:
    settings.birthday_manual_path = tmp_path / "missing.json"
    now = datetime.fromisoformat("2026-07-18T08:00:00+03:00")
    client = FakeBirthdayClient(
        [birthday_contact(102, day=19, month=7)],
        [birthday_user(102, first_name="Маша")],
    )
    await sync_telegram_birthdays(session, client, now)
    email = FakeEmail()

    sent = send_birthday_notifications(session, email, now, lookahead_days=1)

    assert sent == 1
    assert email.sent[0][0] == "[Telegram Detox][ДР] Скоро дни рождения"
    assert "Маша — завтра" in email.sent[0][1]


async def test_duplicate_same_day_notification_is_not_sent(session, settings, tmp_path) -> None:
    settings.birthday_manual_path = tmp_path / "missing.json"
    now = datetime.fromisoformat("2026-07-18T12:00:00+03:00")
    client = FakeBirthdayClient(
        [birthday_contact(103, day=18, month=7)],
        [birthday_user(103, first_name="Анна")],
    )
    await sync_telegram_birthdays(session, client, now)
    email = FakeEmail()

    assert send_birthday_notifications(session, email, now, lookahead_days=1) == 1
    assert send_birthday_notifications(session, email, now, lookahead_days=1) == 0
    assert len(email.sent) == 1


async def test_birthday_message_id_is_stable_and_used(session, settings, tmp_path) -> None:
    settings.birthday_manual_path = tmp_path / "missing.json"
    now = datetime.fromisoformat("2026-07-18T12:00:00+03:00")
    client = FakeBirthdayClient(
        [birthday_contact(109, day=18, month=7)],
        [birthday_user(109, first_name="Stable Contact")],
    )
    await sync_telegram_birthdays(session, client, now)
    due = due_birthdays(session, now.date(), lookahead_days=0)

    expected = birthday_message_id(due)
    assert birthday_message_id(due) == expected

    email = FakeEmail()
    assert send_birthday_notifications(session, email, now, lookahead_days=0) == 1
    assert email.message_ids == [expected]


async def test_ambiguous_birthday_email_failure_is_not_retried(
    session,
    settings,
    tmp_path,
) -> None:
    settings.birthday_manual_path = tmp_path / "missing.json"
    now = datetime.fromisoformat("2026-07-18T12:00:00+03:00")
    client = FakeBirthdayClient(
        [birthday_contact(108, day=18, month=7)],
        [birthday_user(108, first_name="Retry Contact")],
    )
    await sync_telegram_birthdays(session, client, now)

    ambiguous_email = AcceptedThenRaisesEmail()
    with pytest.raises(RuntimeError, match="email unavailable"):
        send_birthday_notifications(session, ambiguous_email, now, lookahead_days=0)

    email = FakeEmail()
    assert send_birthday_notifications(
        session,
        email,
        now + timedelta(minutes=11),
        lookahead_days=0,
    ) == 0
    assert ambiguous_email.provider_accepts == 1
    assert email.sent == []


def test_manual_json_birthday_works(session, settings, tmp_path) -> None:
    path = tmp_path / "birthdays.json"
    path.write_text(
        json.dumps([{"name": "Маша", "date": "07-18", "note": "без года"}]),
        encoding="utf-8",
    )
    settings.birthday_manual_path = path
    now = datetime.fromisoformat("2026-07-18T09:00:00+03:00")
    email = FakeEmail()

    assert sync_manual_birthdays(session, path, now) == 1
    assert send_birthday_notifications(session, email, now, lookahead_days=1) == 1
    assert email.sent[0][0] == "[Telegram Detox][ДР] Сегодня: Маша"
    assert "- manual" in email.sent[0][1]


async def test_telegram_and_manual_sources_merge_without_duplicate_spam(
    session,
    settings,
    tmp_path,
) -> None:
    path = tmp_path / "birthdays.json"
    path.write_text(
        json.dumps(
            [
                {
                    "name": "Иван Петров",
                    "date": "1998-07-18",
                    "telegram": "@ivan",
                }
            ]
        ),
        encoding="utf-8",
    )
    settings.birthday_manual_path = path
    now = datetime.fromisoformat("2026-07-18T12:00:00+03:00")
    client = FakeBirthdayClient(
        [birthday_contact(104, day=18, month=7, year=1998)],
        [birthday_user(104, first_name="Иван", last_name="Петров", username="ivan")],
    )
    await sync_telegram_birthdays(session, client, now)
    sync_manual_birthdays(session, path, now)
    email = FakeEmail()

    assert send_birthday_notifications(session, email, now, lookahead_days=1) == 1
    assert send_birthday_notifications(session, email, now, lookahead_days=1) == 0
    assert email.sent[0][1].count("Иван Петров — сегодня") == 1
    assert "- Telegram / manual" in email.sent[0][1]


async def test_telegram_birthday_year_is_optional(session, settings, tmp_path) -> None:
    settings.birthday_manual_path = tmp_path / "missing.json"
    now = datetime.fromisoformat("2026-07-18T08:00:00+03:00")
    client = FakeBirthdayClient(
        [birthday_contact(105, day=18, month=7, year=None)],
        [birthday_user(105, first_name="Без Года")],
    )

    await sync_telegram_birthdays(session, client, now)

    assert repository.birthday_contacts(session)[0].year is None


def test_feb_29_birthday_falls_back_to_feb_28_in_non_leap_year(
    session,
    settings,
    tmp_path,
) -> None:
    path = tmp_path / "birthdays.json"
    path.write_text(
        json.dumps([{"name": "Леонид", "date": "02-29"}]),
        encoding="utf-8",
    )
    settings.birthday_manual_path = path
    now = datetime.fromisoformat("2027-02-28T09:00:00+03:00")
    sync_manual_birthdays(session, path, now)
    email = FakeEmail()

    assert send_birthday_notifications(session, email, now, lookahead_days=0) == 1
    assert "Леонид — сегодня" in email.sent[0][1]


async def test_no_birthday_sends_no_email(session, settings, tmp_path) -> None:
    settings.birthday_manual_path = tmp_path / "missing.json"
    now = datetime.fromisoformat("2026-07-18T12:00:00+03:00")
    client = FakeBirthdayClient([], [])
    email = FakeEmail()

    stats = await poll_birthdays_once(session, client, email, settings, now)

    assert stats.telegram_contacts == 0
    assert stats.notifications_sent == 0
    assert email.sent == []


async def test_birthday_logs_expose_only_counts(session, settings, tmp_path, caplog) -> None:
    settings.birthday_manual_path = tmp_path / "missing.json"
    now = datetime.fromisoformat("2026-07-18T12:00:00+03:00")
    client = FakeBirthdayClient(
        [birthday_contact(106, day=18, month=7, year=1991)],
        [birthday_user(106, first_name="Очень Секретное Имя")],
    )

    with caplog.at_level(logging.INFO):
        await poll_birthdays_once(session, client, FakeEmail(), settings, now)

    assert "Очень Секретное Имя" not in caplog.text
    assert "1991-07-18" not in caplog.text
    assert "telegram_contacts=1" in caplog.text


async def test_birthday_cli_dry_run_prints_only_counts_and_safe_sample(
    settings,
    tmp_path,
    monkeypatch,
) -> None:
    path = tmp_path / "birthdays.json"
    path.write_text(
        json.dumps([{"name": "Настоящее Секретное Имя", "date": "07-18"}]),
        encoding="utf-8",
    )
    settings.birthday_manual_path = path
    client = FakeBirthdayClient(
        [birthday_contact(107, day=18, month=7)],
        [birthday_user(107, first_name="Другое Секретное Имя")],
    )
    monkeypatch.setattr(check_birthdays, "make_client", lambda _settings: client)
    output: list[str] = []

    await check_birthdays.run(settings, dry_run=True, output=output.append)

    rendered = "\n".join(output)
    assert client.connected is True
    assert client.disconnected is True
    assert "telegram_contacts=1" in rendered
    assert "Пример Контакта" in rendered
    assert "Настоящее Секретное Имя" not in rendered
    assert "Другое Секретное Имя" not in rendered
