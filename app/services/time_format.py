from __future__ import annotations

import re
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

USER_TIMEZONE = ZoneInfo("Europe/Moscow")
UTC_ISO_DATETIME_RE = re.compile(
    r"(?<!\d)"
    r"\d{4}-\d{2}-\d{2}T"
    r"(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d"
    r"(?:\.\d+)?"
    r"(?:\+00:00|Z)"
    r"(?!\d)"
)


def to_user_timezone(value: datetime) -> datetime:
    """Return a Europe/Moscow-aware datetime for user-facing rendering."""
    if value.tzinfo is None or value.utcoffset() is None:
        # SQLite can return naive values for columns that store UTC datetimes.
        # The repository normalizes timestamps to UTC before persisting them.
        value = value.replace(tzinfo=UTC)
    return value.astimezone(USER_TIMEZONE)


def format_user_datetime(value: datetime) -> str:
    return f"{to_user_timezone(value):%Y-%m-%d %H:%M} MSK"


def format_user_time(value: datetime) -> str:
    return f"{to_user_timezone(value):%H:%M}"


def format_user_time_range(start: datetime, end: datetime) -> str:
    return f"{format_user_time(start)}–{format_user_time(end)} MSK"


def format_user_date(value: datetime) -> str:
    return f"{to_user_timezone(value):%Y-%m-%d}"


def localize_embedded_utc_iso(text: str | None) -> str:
    """Localize clear embedded UTC ISO datetimes without changing other date text."""
    if not text:
        return text or ""

    def replace(match: re.Match[str]) -> str:
        raw_value = match.group(0)
        try:
            value = datetime.fromisoformat(raw_value.replace("Z", "+00:00"))
        except ValueError:
            return raw_value
        return format_user_datetime(value)

    return UTC_ISO_DATETIME_RE.sub(replace, text)
