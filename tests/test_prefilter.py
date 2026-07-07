from __future__ import annotations

from app.services.prefilter import is_p0_candidate


def test_call_in_an_hour_is_p0_candidate() -> None:
    assert is_p0_candidate("Позвони через час")

