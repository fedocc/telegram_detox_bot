from __future__ import annotations

import re
from datetime import UTC, datetime

from app.email.render import render_html, render_plain_text
from app.models.schemas import DailyDigest
from app.services.time_format import (
    format_user_date,
    format_user_datetime,
    format_user_time,
    format_user_time_range,
    localize_embedded_utc_iso,
    to_user_timezone,
)


def test_user_time_formatters_convert_utc_to_moscow() -> None:
    exact = datetime.fromisoformat("2026-07-20T09:26:04+00:00")
    context = datetime.fromisoformat("2026-07-20T02:57:00+00:00")
    start = datetime.fromisoformat("2026-07-20T06:11:00+00:00")
    end = datetime.fromisoformat("2026-07-20T07:14:00+00:00")

    assert format_user_datetime(exact) == "2026-07-20 12:26 MSK"
    assert format_user_time(context) == "05:57"
    assert format_user_time_range(start, end) == "09:11–10:14 MSK"
    assert to_user_timezone(exact).tzinfo is not UTC


def test_naive_repository_timestamp_is_treated_as_utc() -> None:
    naive_utc = datetime(2026, 7, 20, 9, 26, 4)

    assert format_user_datetime(naive_utc) == "2026-07-20 12:26 MSK"


def test_user_date_uses_moscow_calendar_day() -> None:
    late_utc = datetime.fromisoformat("2026-07-19T22:30:00+00:00")

    assert format_user_date(late_utc) == "2026-07-20"


def test_embedded_utc_iso_timestamps_are_localized_conservatively() -> None:
    text = (
        "до 2026-07-20T09:26:04+00:00; "
        "deadline: 2026-07-19T22:30:00.123456Z; "
        "дата 2026-07-20; local 2026-07-20T09:26:04+03:00"
    )

    assert localize_embedded_utc_iso(text) == (
        "до 2026-07-20 12:26 MSK; "
        "deadline: 2026-07-20 01:30 MSK; "
        "дата 2026-07-20; local 2026-07-20T09:26:04+03:00"
    )
    assert localize_embedded_utc_iso(None) == ""
    assert localize_embedded_utc_iso("") == ""
    assert localize_embedded_utc_iso("2026-07-20T09:26:04") == "2026-07-20T09:26:04"


def test_all_digest_sections_render_moscow_times_without_raw_utc_iso() -> None:
    start = datetime.fromisoformat("2026-07-20T06:11:00+00:00")
    end = datetime.fromisoformat("2026-07-20T07:14:00+00:00")
    shared = {
        "summary": "Synthetic summary.",
        "first_message_at": start,
        "last_message_at": end,
    }
    digest = DailyDigest(
        date=format_user_date(datetime.fromisoformat("2026-07-19T22:30:00+00:00")),
        p0_alerts=[
            {
                "chat": "Synthetic urgent chat",
                "alert_sent": True,
                "deadline_at": datetime.fromisoformat("2026-07-20T09:26:04+00:00"),
                **shared,
            }
        ],
        direct_messages=[
            {"chat": "Synthetic private chat", "needs_reply": False, **shared}
        ],
        group_updates=[{"chat": "Synthetic group", **shared}],
        channel_updates=[{"chat": "Synthetic channel", **shared}],
    )

    rendered = render_plain_text(digest) + render_html(digest)

    assert rendered.count("Время: 09:11–10:14 MSK") == 8
    assert "Срок: 2026-07-20 12:26 MSK" in rendered
    assert "2026-07-20" in rendered
    assert "+00:00" not in rendered
    assert not re.search(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00", rendered)
