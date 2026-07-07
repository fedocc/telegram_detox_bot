from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.models.schemas import DailyDigest


def test_llm_json_is_validated_by_pydantic_model() -> None:
    raw = '{"date":"2026-07-07","noise_counts":[{"chat":"Общий","count":43}]}'

    digest = DailyDigest.model_validate_json(raw)

    assert digest.noise_counts[0].count == 43


def test_invalid_llm_json_fails_validation() -> None:
    raw = '{"date":"2026-07-07","noise_counts":[{"chat":"Общий","count":-1}]}'

    with pytest.raises(ValidationError):
        DailyDigest.model_validate_json(raw)

