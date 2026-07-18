from __future__ import annotations

import json
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
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


class SequenceCompletions:
    def __init__(self, responses) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
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


def digest_payload(text: str = "ответь через час") -> dict:
    return {
        "date": "2026-07-07",
        "chats": [
            {
                "chat_id": "1",
                "chat_title": "Маша",
                "chat_type": "private",
                "messages": [
                    {
                        "text": text,
                        "source_ref": {"chat_id": "1", "message_id": 1},
                    }
                ],
            }
        ],
    }


def digest_json(
    *,
    summary: str = "Просит ответить через час.",
    deadlines: list[str] | None = None,
) -> str:
    return json.dumps(
        {
            "items": [
                {
                    "chat_id": "1",
                    "chat_title": "Маша",
                    "chat_type": "private",
                    "summary": summary,
                    "requests": ["ответить"],
                    "context": ["сообщение требует ответа"],
                    "actions": ["ответить в Telegram"],
                    "deadlines": deadlines or [],
                    "open_telegram": True,
                    "reason_to_open": "Нужен ответ.",
                    "message_count": 1,
                }
            ]
        },
        ensure_ascii=False,
    )


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


def test_p0_prompt_and_user_payload_include_trusted_sender_context(settings: Settings) -> None:
    completions = MalformedCompletions(response_with(p0_json("NOT_P0")))
    client = HaikuClient(settings)
    client.client = FakeClient(completions)

    client.classify_p0(
        {
            "message": {
                "text": "пришли договор сейчас",
                "trusted_sender": True,
            },
            "context": [],
        }
    )

    messages = completions.calls[0]["messages"]
    assert "message.trusted_sender" in messages[0]["content"]
    assert "never makes obvious small talk P0" in messages[0]["content"]
    assert "Prioritize recall" in messages[0]["content"]
    assert '"trusted_sender": true' in messages[1]["content"]


def test_json_fence_response_parses(settings: Settings) -> None:
    client = HaikuClient(settings)
    client.client = FakeClient(MalformedCompletions(response_with(f"```json\n{p0_json()}\n```")))

    result = client.classify_p0({"message": {"text": "ping"}, "context": []})

    assert result.status == "P0_STRICT"


def test_plain_fence_response_parses(settings: Settings) -> None:
    client = HaikuClient(settings)
    client.client = FakeClient(MalformedCompletions(response_with(f"```\n{p0_json()}\n```")))

    result = client.classify_p0({"message": {"text": "ping"}, "context": []})

    assert result.status == "P0_STRICT"


def test_whitespace_around_json_parses(settings: Settings) -> None:
    client = HaikuClient(settings)
    client.client = FakeClient(MalformedCompletions(response_with(f"\n\n  {p0_json()}  \n")))

    result = client.classify_p0({"message": {"text": "ping"}, "context": []})

    assert result.status == "P0_STRICT"


def test_repair_json_fence_response_parses(settings: Settings) -> None:
    client = HaikuClient(settings)
    client.client = FakeClient(
        SequenceCompletions([
            response_with("{bad json}"),
            response_with(f"```json\n{p0_json('REVIEW')}\n```"),
        ])
    )

    result = client.classify_p0({"message": {"text": "ping"}, "context": []})

    assert result.status == "P0_CANDIDATE"


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
        MalformedCompletions(response_with(digest_json(deadlines=["через час"])))
    )

    digest = client.daily_digest(digest_payload())

    assert digest.direct_messages[0].deadline_text == "через час"
    assert digest.direct_messages[0].deadline_at is None


def test_valid_llm_digest_uses_simple_schema(settings: Settings) -> None:
    completions = MalformedCompletions(response_with(digest_json()))
    client = HaikuClient(settings)
    client.client = FakeClient(completions)

    digest = client.daily_digest(digest_payload())

    assert digest.generated_by == "llm"
    assert digest.direct_messages[0].summary == "Просит ответить через час."
    assert digest.direct_messages[0].source_refs == [{"chat_id": "1", "message_id": 1}]
    assert completions.calls[0]["response_format"]["type"] == "json_schema"


def test_invalid_llm_digest_triggers_explicit_repair(settings: Settings) -> None:
    incomplete = '{"items":[{"chat_id":"1","summary":"Нужен ответ."}]}'
    completions = SequenceCompletions(
        [response_with(incomplete), response_with(digest_json())]
    )
    client = HaikuClient(settings)
    client.client = FakeClient(completions)

    digest = client.daily_digest(digest_payload())

    assert digest.direct_messages[0].summary == "Просит ответить через час."
    assert len(completions.calls) == 2
    repair_prompt = completions.calls[1]["messages"][1]["content"]
    assert "REQUIRED_SCHEMA" in repair_prompt
    assert "ORIGINAL_INPUT" in repair_prompt
    assert "PREVIOUS_OUTPUT" in repair_prompt
    assert "No markdown" in repair_prompt


def test_count_only_llm_summary_triggers_repair(settings: Settings) -> None:
    completions = SequenceCompletions(
        [
            response_with(digest_json(summary="5 сообщений")),
            response_with(digest_json(summary="Просит подтвердить время встречи.")),
        ]
    )
    client = HaikuClient(settings)
    client.client = FakeClient(completions)

    digest = client.daily_digest(digest_payload())

    assert digest.direct_messages[0].summary == "Просит подтвердить время встречи."
    assert len(completions.calls) == 2


def test_digest_markdown_fence_is_repaired_to_strict_json(settings: Settings) -> None:
    completions = SequenceCompletions(
        [
            response_with(f"```json\n{digest_json()}\n```"),
            response_with(digest_json()),
        ]
    )
    client = HaikuClient(settings)
    client.client = FakeClient(completions)

    digest = client.daily_digest(digest_payload())

    assert digest.direct_messages[0].summary == "Просит ответить через час."
    assert len(completions.calls) == 2


def test_missing_chat_summary_after_repair_is_rejected(settings: Settings) -> None:
    payload = digest_payload()
    payload["chats"].append(
        {
            "chat_id": "2",
            "chat_title": "Рабочая группа",
            "chat_type": "group",
            "messages": [
                {
                    "text": "обновление",
                    "source_ref": {"chat_id": "2", "message_id": 1},
                }
            ],
        }
    )
    client = HaikuClient(settings)
    client.client = FakeClient(MalformedCompletions(response_with(digest_json())))

    with pytest.raises(LLMError) as error:
        client.daily_digest(payload)

    assert error.value.reason_code == "validation_failed"
    assert error.value.validation_error_type == "DigestChatCoverageError"


def test_daily_digest_missing_fields_after_repair_becomes_llm_error(
    settings: Settings,
) -> None:
    client = HaikuClient(settings)
    client.client = FakeClient(
        MalformedCompletions(
            response_with('{"items":[{"chat_id":"1","summary":"Есть сообщение."}]}')
        )
    )

    with pytest.raises(LLMError, match="invalid JSON") as error:
        client.daily_digest(digest_payload())

    assert error.value.reason_code == "validation_failed"
    assert error.value.validation_error_type == "ValidationError"


def test_digest_models_use_deadline_text_and_deadline_at() -> None:
    for model in [
        DailyDigest.model_fields["p0_alerts"].annotation.__args__[0],
        DailyDigest.model_fields["direct_messages"].annotation.__args__[0],
        DailyDigest.model_fields["group_updates"].annotation.__args__[0],
    ]:
        assert "deadline" not in model.model_fields
        assert "deadline_text" in model.model_fields
        assert "deadline_at" in model.model_fields
