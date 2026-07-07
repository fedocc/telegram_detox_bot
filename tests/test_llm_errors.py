from __future__ import annotations

import logging

import pytest
from openai import APIConnectionError

from app.config import Settings
from app.llm.client import HaikuClient, LLMError
from app.models.schemas import DailyDigest


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


def p0_json(status: str = "P0") -> str:
    return f'{{"status":"{status}","summary":"ok","confidence":1}}'


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


def test_plain_json_response_parses(settings: Settings) -> None:
    client = HaikuClient(settings)
    client.client = FakeClient(MalformedCompletions(response_with(p0_json("NOT_P0"))))

    result = client.classify_p0({"message": {"text": "ping"}, "context": []})

    assert result.status == "NOT_P0"


def test_json_fence_response_parses(settings: Settings) -> None:
    client = HaikuClient(settings)
    client.client = FakeClient(MalformedCompletions(response_with(f"```json\n{p0_json()}\n```")))

    result = client.classify_p0({"message": {"text": "ping"}, "context": []})

    assert result.status == "P0"


def test_plain_fence_response_parses(settings: Settings) -> None:
    client = HaikuClient(settings)
    client.client = FakeClient(MalformedCompletions(response_with(f"```\n{p0_json()}\n```")))

    result = client.classify_p0({"message": {"text": "ping"}, "context": []})

    assert result.status == "P0"


def test_whitespace_around_json_parses(settings: Settings) -> None:
    client = HaikuClient(settings)
    client.client = FakeClient(MalformedCompletions(response_with(f"\n\n  {p0_json()}  \n")))

    result = client.classify_p0({"message": {"text": "ping"}, "context": []})

    assert result.status == "P0"


def test_repair_json_fence_response_parses(settings: Settings) -> None:
    client = HaikuClient(settings)
    client.client = FakeClient(
        SequenceCompletions([
            response_with("{bad json}"),
            response_with(f"```json\n{p0_json('REVIEW')}\n```"),
        ])
    )

    result = client.classify_p0({"message": {"text": "ping"}, "context": []})

    assert result.status == "REVIEW"


def test_text_before_json_is_rejected(settings: Settings) -> None:
    client = HaikuClient(settings)
    client.client = FakeClient(MalformedCompletions(response_with(f"Here is JSON:\n{p0_json()}")))

    with pytest.raises(LLMError):
        client.classify_p0({"message": {"text": "ping"}, "context": []})


def test_multiple_code_blocks_are_rejected(settings: Settings) -> None:
    client = HaikuClient(settings)
    client.client = FakeClient(
        MalformedCompletions(response_with(f"```json\n{p0_json()}\n```\n```json\n{p0_json()}\n```"))
    )

    with pytest.raises(LLMError):
        client.classify_p0({"message": {"text": "ping"}, "context": []})


def test_invalid_fenced_json_becomes_llm_error(settings: Settings) -> None:
    client = HaikuClient(settings)
    client.client = FakeClient(MalformedCompletions(response_with("```json\n{bad json\n```")))

    with pytest.raises(LLMError):
        client.classify_p0({"message": {"text": "ping"}, "context": []})


def test_relative_deadline_becomes_deadline_text(settings: Settings) -> None:
    client = HaikuClient(settings)
    client.client = FakeClient(
        MalformedCompletions(
            response_with('{"status":"P0","summary":"ok","deadline_at":null,'
                          '"deadline_text":"через час","confidence":0.95}')
        )
    )

    result = client.classify_p0({"message": {"text": "позвони через час"}, "context": []})

    assert result.deadline_text == "через час"
    assert result.deadline_at is None


def test_valid_iso_deadline_remains_deadline_at(settings: Settings) -> None:
    client = HaikuClient(settings)
    client.client = FakeClient(
        MalformedCompletions(
            response_with('{"status":"P0","summary":"ok",'
                          '"deadline_at":"2026-07-07T19:00:00+03:00",'
                          '"deadline_text":"сегодня в 19:00","confidence":0.95}')
        )
    )

    result = client.classify_p0({"message": {"text": "позвони сегодня в 19:00"}, "context": []})

    assert result.deadline_at is not None
    assert result.deadline_at.isoformat() == "2026-07-07T19:00:00+03:00"
    assert result.deadline_text == "сегодня в 19:00"


def test_invalid_deadline_at_moves_to_deadline_text(settings: Settings) -> None:
    client = HaikuClient(settings)
    client.client = FakeClient(
        MalformedCompletions(
            response_with('{"status":"P0","summary":"ok","deadline_at":"через час",'
                          '"deadline_text":null,"confidence":0.95}')
        )
    )

    result = client.classify_p0({"message": {"text": "позвони через час"}, "context": []})

    assert result.deadline_text == "через час"
    assert result.deadline_at is None


def test_legacy_deadline_field_is_normalized(settings: Settings) -> None:
    client = HaikuClient(settings)
    client.client = FakeClient(
        MalformedCompletions(
            response_with('{"status":"P0","summary":"ok","deadline":"1 hour","confidence":0.95}')
        )
    )

    result = client.classify_p0({"message": {"text": "call in 1 hour"}, "context": []})

    assert result.deadline_text == "1 hour"
    assert result.deadline_at is None


def test_p0_relative_deadline_does_not_trigger_llm_error(settings: Settings) -> None:
    client = HaikuClient(settings)
    client.client = FakeClient(
        MalformedCompletions(
            response_with('{"status":"P0","summary":"ok","deadline":"1 hour","confidence":0.95}')
        )
    )

    result = client.classify_p0({"message": {"text": "call in 1 hour"}, "context": []})

    assert result.is_p0


def test_daily_digest_relative_deadline_does_not_fail(settings: Settings) -> None:
    client = HaikuClient(settings)
    client.client = FakeClient(
        MalformedCompletions(
            response_with(
                '{"date":"2026-07-07","direct_messages":[{"chat":"Маша",'
                '"summary":"Просит ответить через час.","needs_reply":true,'
                '"deadline_at":"через час","deadline_text":null,'
                '"source_refs":[{"chat_id":"1","message_id":1}]}]}'
            )
        )
    )

    digest = client.daily_digest({"date": "2026-07-07", "chats": []})

    assert digest.direct_messages[0].deadline_text == "через час"
    assert digest.direct_messages[0].deadline_at is None


def test_digest_models_use_deadline_text_and_deadline_at() -> None:
    for model in [
        DailyDigest.model_fields["p0_alerts"].annotation.__args__[0],
        DailyDigest.model_fields["direct_messages"].annotation.__args__[0],
        DailyDigest.model_fields["group_updates"].annotation.__args__[0],
    ]:
        assert "deadline" not in model.model_fields
        assert "deadline_text" in model.model_fields
        assert "deadline_at" in model.model_fields
