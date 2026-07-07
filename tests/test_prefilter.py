from __future__ import annotations

from app.services.prefilter import is_p0_candidate


def test_call_in_an_hour_is_p0_candidate() -> None:
    assert is_p0_candidate("Позвони через час")


def test_p0_prefilter_handles_russian_time_variants() -> None:
    assert is_p0_candidate("через 15 минут")
    assert is_p0_candidate("через 2 часа")
    assert is_p0_candidate("до 18:30")
    assert is_p0_candidate("сегодня до 19:00")
    assert is_p0_candidate("завтра в 10:30")


def test_p0_prefilter_handles_asap_variants() -> None:
    assert is_p0_candidate("asap")
    assert is_p0_candidate("ASAP please")
    assert is_p0_candidate("as soon as possible")


def test_p0_prefilter_respects_moscow_timezone() -> None:
    from app.config import Settings

    assert Settings().timezone == "Europe/Moscow"
