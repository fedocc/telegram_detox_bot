from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from datetime import datetime
from typing import TypeVar

from openai import OpenAI, OpenAIError
from pydantic import BaseModel, TypeAdapter, ValidationError

from app.config import Settings
from app.models.schemas import (
    DailyDigest,
    DigestDirectMessage,
    DigestGroupUpdate,
    DigestLLMResponse,
    P0Decision,
)

T = TypeVar("T", bound=BaseModel)
logger = logging.getLogger(__name__)
COUNT_ONLY_DIGEST_SUMMARY_RE = re.compile(
    r"^(?:(?:всего\s+)?\d+\s+(?:новых\s+)?(?:сообщени(?:е|я|й)|messages?)|"
    r"(?:сообщени(?:е|я|й)|messages?)\s*:\s*\d+)[.!]?$",
    re.IGNORECASE,
)


class LLMError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        reason_code: str = "llm_error",
        validation_error_type: str | None = None,
    ) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.validation_error_type = validation_error_type


class DigestChatCoverageError(ValueError):
    pass


class DigestCountOnlySummaryError(ValueError):
    pass


def _safe_llm_error(exc: Exception) -> LLMError:
    return LLMError(
        f"llm_error:{exc.__class__.__name__}",
        reason_code="provider_error",
    )


def _validation_error_type(exc: Exception) -> str:
    return exc.__class__.__name__


def _extract_content(response) -> str:
    try:
        choices = response.choices
        choice = choices[0]
        content = choice.message.content
    except Exception as exc:
        raise _safe_llm_error(exc) from exc
    if not isinstance(content, str) or not content:
        raise LLMError("llm_error:MalformedResponse")
    return content


def _extract_json_document(content: str, *, allow_code_fences: bool = True) -> str:
    text = content.strip()
    if text.startswith("```"):
        if not allow_code_fences:
            raise LLMError("llm_error:InvalidJsonEnvelope")
        lines = text.splitlines()
        if len(lines) < 3:
            raise LLMError("llm_error:InvalidJsonEnvelope")
        opening = lines[0].strip().lower()
        closing = lines[-1].strip()
        if opening not in {"```", "```json"} or closing != "```":
            raise LLMError("llm_error:InvalidJsonEnvelope")
        body = "\n".join(lines[1:-1]).strip()
        if "```" in body:
            raise LLMError("llm_error:InvalidJsonEnvelope")
        text = body
    if not text.startswith("{") or not text.endswith("}"):
        raise LLMError("llm_error:InvalidJsonEnvelope")
    return text


def _is_valid_datetime(value: object) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        TypeAdapter(datetime).validate_python(value)
    except (TypeError, ValueError, ValidationError):
        return False
    return True


def _normalize_deadlines(value: object) -> object:
    if isinstance(value, list):
        return [_normalize_deadlines(item) for item in value]
    if not isinstance(value, dict):
        return value

    normalized = {key: _normalize_deadlines(item) for key, item in value.items()}
    if normalized.get("status") == "P0":
        normalized["status"] = "P0_STRICT"
    elif normalized.get("status") == "REVIEW":
        normalized["status"] = "P0_CANDIDATE"
    legacy_deadline = normalized.pop("deadline", None)
    if legacy_deadline is not None:
        if _is_valid_datetime(legacy_deadline):
            normalized.setdefault("deadline_at", legacy_deadline)
        elif not normalized.get("deadline_text"):
            normalized["deadline_text"] = str(legacy_deadline)
        normalized.setdefault("deadline_at", None)

    deadline_at = normalized.get("deadline_at")
    if deadline_at is not None and not _is_valid_datetime(deadline_at):
        if not normalized.get("deadline_text"):
            normalized["deadline_text"] = str(deadline_at)
        normalized["deadline_at"] = None

    return normalized


def _normalize_json_document(content: str) -> str:
    document = json.loads(content)
    if not isinstance(document, dict):
        raise ValueError("LLM JSON root must be an object")
    return json.dumps(_normalize_deadlines(document), ensure_ascii=False)


class HaikuClient:
    def __init__(self, settings: Settings):
        if not settings.aitunnel_api_key:
            raise LLMError("AITUNNEL_API_KEY is not configured")
        self.settings = settings
        self.client = OpenAI(api_key=settings.aitunnel_api_key, base_url=settings.aitunnel_base_url)

    def _json_completion(
        self,
        system: str,
        user: str,
        schema: type[BaseModel],
        *,
        prefer_schema: bool = False,
    ) -> str:
        response_format: dict = {"type": "json_object"}
        if prefer_schema:
            response_format = {
                "type": "json_schema",
                "json_schema": {
                    "name": schema.__name__,
                    "schema": schema.model_json_schema(),
                    "strict": True,
                },
            }
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        try:
            try:
                response = self.client.chat.completions.create(
                    model=self.settings.aitunnel_model,
                    messages=messages,
                    response_format=response_format,
                    temperature=0,
                )
            except OpenAIError as first_error:
                if not prefer_schema:
                    raise _safe_llm_error(first_error) from first_error
                try:
                    response = self.client.chat.completions.create(
                        model=self.settings.aitunnel_model,
                        messages=messages,
                        response_format={"type": "json_object"},
                        temperature=0,
                    )
                except Exception as retry_error:
                    raise _safe_llm_error(retry_error) from retry_error
        except (TimeoutError, OSError) as exc:
            raise _safe_llm_error(exc) from exc
        except Exception as exc:
            if isinstance(exc, LLMError):
                raise
            raise _safe_llm_error(exc) from exc
        return _extract_content(response)

    def _validated_json(
        self,
        schema: type[T],
        system: str,
        user: str,
        *,
        validate: Callable[[T], T] | None = None,
        prefer_schema: bool = False,
        allow_code_fences: bool = True,
    ) -> T:
        raw = self._json_completion(system, user, schema, prefer_schema=prefer_schema)

        def parse(content: str) -> T:
            json_text = _extract_json_document(
                content,
                allow_code_fences=allow_code_fences,
            )
            result = schema.model_validate_json(_normalize_json_document(json_text))
            return validate(result) if validate else result

        try:
            return parse(raw)
        except (LLMError, ValidationError, ValueError, json.JSONDecodeError) as first_error:
            first_error_type = _validation_error_type(first_error)
            logger.warning(
                "LLM JSON invalid; repair retry scheduled validation_error_type=%s",
                first_error_type,
            )
            schema_json = json.dumps(schema.model_json_schema(), ensure_ascii=False)
            repair_user = (
                "Convert PREVIOUS_OUTPUT into valid JSON matching REQUIRED_SCHEMA exactly. "
                "Preserve the meaning, add every required field, and remove unknown fields. "
                "Return one JSON object only. No markdown, code fences, explanations, or prose.\n"
                f"REQUIRED_SCHEMA:\n{schema_json}\n"
                f"ORIGINAL_INPUT:\n{user}\n"
                f"PREVIOUS_OUTPUT:\n{raw}"
            )
            try:
                repaired = self._json_completion(
                    system,
                    repair_user,
                    schema,
                    prefer_schema=prefer_schema,
                )
            except Exception as repair_error:
                raise _safe_llm_error(repair_error) from repair_error
            try:
                return parse(repaired)
            except (
                LLMError,
                ValidationError,
                ValueError,
                json.JSONDecodeError,
            ) as second_error:
                second_error_type = _validation_error_type(second_error)
                raise LLMError(
                    "LLM returned invalid JSON after repair retry",
                    reason_code="validation_failed",
                    validation_error_type=second_error_type,
                ) from second_error

    def classify_p0(self, payload: dict) -> P0Decision:
        message = payload.get("message", {}) if isinstance(payload, dict) else {}
        reference_timestamp = message.get("timestamp") or "unknown"
        system = (
            "Lightweight Telegram P0 classifier. Return JSON only with keys: "
            "status, summary, reason, action, deadline_text, deadline_at, confidence. status must "
            "be P0_STRICT, P0_CANDIDATE, or NOT_P0. Prioritize recall: missing a message that may "
            "need a reaction is worse than an extra alert. In private chats, use P0_STRICT for "
            "requests, questions about plans or availability, contact requests, important "
            "context, and borderline cases where a response may be expected. Keep obvious small "
            "talk and casual updates in NOT_P0. A question mark alone is not P0 in a private "
            "chat; phrases such as how are you or как дела remain small talk. In groups, use "
            "P0_STRICT for an exact direct "
            "mention, any reply to the user, urgency, importance, deadlines, requests/actions, "
            "watchlist matches, or borderline directed questions. Read authoritative local facts "
            "from message.policy and trusted status from message.trusted_sender. A trusted sender "
            "never makes obvious small talk P0. If uncertain whether a reaction is expected, use "
            "P0_STRICT; otherwise use P0_CANDIDATE. "
            f"Reference timestamp: {reference_timestamp}. Timezone: {self.settings.timezone}. "
            "deadline_at must be exact ISO 8601 datetime or null. "
            "Put relative or human deadline wording only into deadline_text. "
            "Return only valid JSON. No markdown. No code fences. No explanation."
        )
        return self._validated_json(P0Decision, system, json.dumps(payload, ensure_ascii=False))

    def daily_digest(self, payload: dict) -> DailyDigest:
        reference_timestamp = payload.get("reference_timestamp") or payload.get("date") or "unknown"
        chats = payload.get("chats") if isinstance(payload.get("chats"), list) else []
        chats_by_id = {
            str(chat.get("chat_id")): chat
            for chat in chats
            if isinstance(chat, dict) and chat.get("chat_id") is not None
        }
        expected_chat_ids = set(chats_by_id)

        def normalized_chat_type(chat: dict) -> str:
            chat_type = str(chat.get("chat_type") or "channel")
            return "group" if chat_type == "supergroup" else chat_type

        def validate_response(response: DigestLLMResponse) -> DigestLLMResponse:
            actual_chat_ids = {item.chat_id for item in response.items}
            if actual_chat_ids != expected_chat_ids:
                raise DigestChatCoverageError("digest chat coverage mismatch")
            for item in response.items:
                if COUNT_ONLY_DIGEST_SUMMARY_RE.fullmatch(item.summary.strip()):
                    raise DigestCountOnlySummaryError("count-only digest summary")
                if item.chat_type != normalized_chat_type(chats_by_id[item.chat_id]):
                    raise DigestChatCoverageError("digest chat type mismatch")
            return response

        system = (
            "Create a concise practical Telegram digest. Return strict JSON only with this exact "
            "shape: {\"items\":[{\"chat_id\":\"string\",\"chat_title\":\"string\","
            "\"chat_type\":\"private|group|channel\",\"summary\":\"string\","
            "\"requests\":[\"string\"],\"context\":[\"string\"],"
            "\"actions\":[\"string\"],\"deadlines\":[\"string\"],"
            "\"open_telegram\":true,\"reason_to_open\":\"string\","
            "\"message_count\":1}]}. "
            "Include exactly one item for every input chat_id and never combine chats. Preserve "
            "chat_id and chat_type exactly. Every key is required; use empty arrays when there "
            "are no requests, context, actions, or deadlines. Summaries must explain what "
            "happened and must not be only a message count. Media is already collapsed. "
            "Keep Russian summaries concise and action-oriented. "
            f"Reference timestamp: {reference_timestamp}. Timezone: {self.settings.timezone}. "
            "No markdown. No code fences. No prose before or after the JSON object."
        )
        response = self._validated_json(
            DigestLLMResponse,
            system,
            json.dumps(payload, ensure_ascii=False),
            validate=validate_response,
            prefer_schema=True,
            allow_code_fences=False,
        )
        digest = DailyDigest(date=str(payload.get("date") or ""))
        for item in response.items:
            chat = chats_by_id[item.chat_id]
            messages = chat.get("messages") if isinstance(chat.get("messages"), list) else []
            source_refs = [
                message["source_ref"]
                for message in messages
                if isinstance(message, dict) and isinstance(message.get("source_ref"), dict)
            ]
            requests = "; ".join(item.requests) or "Явных запросов нет."
            context = "; ".join(item.context) or "Дополнительный контекст не выделен."
            actions = "; ".join(item.actions) or "Действий по переписке не указано."
            deadlines = "; ".join(item.deadlines) or None
            common = {
                "chat": str(chat.get("chat_title") or item.chat_title),
                "summary": item.summary,
                "what_happened": item.summary,
                "requests_to_me": requests,
                "important_context": context,
                "action_items": actions,
                "should_open_telegram": item.open_telegram,
                "open_reason": item.reason_to_open,
                "open_telegram": item.open_telegram,
                "deadline_text": deadlines,
                "source_refs": source_refs,
                "message_count": len(messages),
            }
            if item.chat_type == "private":
                digest.direct_messages.append(
                    DigestDirectMessage(
                        needs_reply=bool(item.requests or item.actions),
                        **common,
                    )
                )
            else:
                digest.group_updates.append(DigestGroupUpdate(**common))
        return digest
