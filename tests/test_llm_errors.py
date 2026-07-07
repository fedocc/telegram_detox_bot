from __future__ import annotations

import logging

import pytest
from openai import APIConnectionError

from app.config import Settings
from app.llm.client import HaikuClient, LLMError


class BrokenCompletions:
    def create(self, **kwargs):
        raise APIConnectionError(request=None)


class BrokenClient:
    def __init__(self) -> None:
        self.chat = type("Chat", (), {"completions": BrokenCompletions()})()


def test_second_openai_retry_error_becomes_llm_error(settings: Settings) -> None:
    client = HaikuClient(settings)
    client.client = BrokenClient()

    with pytest.raises(LLMError):
        client.classify_p0({"message": {"text": "ping"}, "context": []})


def test_llm_error_does_not_log_secret_like_provider_details(settings: Settings, caplog) -> None:
    client = HaikuClient(settings)
    client.client = BrokenClient()

    with caplog.at_level(logging.WARNING):
        with pytest.raises(LLMError):
            client.classify_p0({"message": {"text": "token=secret"}, "context": []})

    assert "token=secret" not in caplog.text
