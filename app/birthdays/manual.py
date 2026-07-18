from __future__ import annotations

import json
import re
from datetime import date
from hashlib import sha256
from pathlib import Path

from app.birthdays.models import BirthdaySourceRecord


class BirthdayDataError(ValueError):
    pass


def _clean_text(value: object, *, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:limit]


def _parse_date(value: object, item_number: int) -> tuple[int, int, int | None]:
    if not isinstance(value, str):
        raise BirthdayDataError(f"Manual birthday item {item_number} has an invalid date")
    full = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", value)
    partial = re.fullmatch(r"(\d{2})-(\d{2})", value)
    try:
        if full:
            year, month, day = (int(part) for part in full.groups())
            date(year, month, day)
            return day, month, year
        if partial:
            month, day = (int(part) for part in partial.groups())
            date(2000, month, day)
            return day, month, None
    except ValueError as exc:
        raise BirthdayDataError(
            f"Manual birthday item {item_number} has an invalid date"
        ) from exc
    raise BirthdayDataError(f"Manual birthday item {item_number} has an invalid date")


def _manual_key(name: str, date_value: str, username: str | None) -> str:
    identity = "|".join((name.casefold(), date_value, (username or "").casefold()))
    return f"manual:{sha256(identity.encode('utf-8')).hexdigest()}"


def load_manual_birthdays(path: Path) -> list[BirthdaySourceRecord]:
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BirthdayDataError("Manual birthday file is unreadable or invalid JSON") from exc
    if not isinstance(payload, list):
        raise BirthdayDataError("Manual birthday file must contain a JSON list")

    records: list[BirthdaySourceRecord] = []
    for index, item in enumerate(payload, start=1):
        if not isinstance(item, dict):
            raise BirthdayDataError(f"Manual birthday item {index} must be an object")
        name = _clean_text(item.get("name"), limit=512)
        if not name:
            raise BirthdayDataError(f"Manual birthday item {index} has no name")
        date_value = _clean_text(item.get("date"), limit=10)
        day, month, year = _parse_date(date_value, index)
        username = _clean_text(item.get("telegram"), limit=128).removeprefix("@") or None
        records.append(
            BirthdaySourceRecord(
                person_key=_manual_key(name, date_value, username),
                telegram_user_id=None,
                display_name=name,
                username=username,
                day=day,
                month=month,
                year=year,
                source="manual",
            )
        )
    return records

