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


class MalformedCompletions:
    def __init__(self, response) -> None:
        self.response = response

    def create(self, **kwargs):
        return self.response


class SequenceCompletions:
    def __init__(self, responses) -> None:
        self.responses = list(responses)

    def create(self, **kwargs):
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class FakeClient:
    def __init__(self, completions) -> None:
        self.chat = type("Chat", (), {"completions": completions})()


def response_with(content):
    message = type("Message", (), {"content": content})()
    choice = type("Choice", (), {"message": message})()
    return type("Response", (), {"choices": [choice]})()


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


def test_empty_choices_becomes_llm_error(settings: Settings) -> None:
    client = HaikuClient(settings)
    client.client = FakeClient(MalformedCompletions(type("Response", (), {"choices": []})()))

    with pytest.raises(LLMError):
        client.classify_p0({"message": {"text": "ping"}, "context": []})


def test_missing_message_content_becomes_llm_error(settings: Settings) -> None:
    client = HaikuClient(settings)
    choice = type("Choice", (), {})()
    client.client = FakeClient(MalformedCompletions(type("Response", (), {"choices": [choice]})()))

    with pytest.raises(LLMError):
        client.classify_p0({"message": {"text": "ping"}, "context": []})


def test_none_content_becomes_llm_error(settings: Settings) -> None:
    client = HaikuClient(settings)
    client.client = FakeClient(MalformedCompletions(response_with(None)))

    with pytest.raises(LLMError):
        client.classify_p0({"message": {"text": "ping"}, "context": []})


def test_second_retry_malformed_response_becomes_llm_error(settings: Settings) -> None:
    client = HaikuClient(settings)
    client.client = FakeClient(
        SequenceCompletions([
            response_with("{bad json"),
            type("Response", (), {"choices": []})(),
        ])
    )

    with pytest.raises(LLMError):
        client.classify_p0({"message": {"text": "ping"}, "context": []})


def test_openai_sdk_error_is_wrapped_as_llm_error(settings: Settings) -> None:
    client = HaikuClient(settings)
    client.client = BrokenClient()

    with pytest.raises(LLMError, match="llm_error"):
        client.daily_digest({"date": "2026-07-07", "chats": []})
