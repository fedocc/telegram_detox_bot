from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
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
TRUSTED_DIGEST_SCHEMA_FIELDS = frozenset(
    {
        "items",
        "chat_id",
        "summary",
        "requests",
        "context",
        "actions",
        "deadlines",
        "open_telegram",
        "reason_to_open",
    }
)
TRUSTED_VALIDATION_ERROR_TYPES = frozenset(
    {
        "ValidationError",
        "DigestChatCoverageError",
        "DigestCountOnlySummaryError",
        "JSONDecodeError",
        "LLMError",
        "ValueError",
    }
)
TRUSTED_VALIDATION_ERROR_CODES = frozenset(
    {
        "missing",
        "string_type",
        "list_type",
        "bool_type",
        "bool_parsing",
        "literal_error",
        "extra_forbidden",
        "int_type",
        "int_parsing",
        "greater_than_equal",
        "value_error",
        "json_invalid",
        "invalid_json_envelope",
        "duplicate_chat_id",
        "missing_expected_chat",
        "unexpected_chat_id",
        "count_only_summary",
        "validation_error",
    }
)


class LLMError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        reason_code: str = "llm_error",
        validation_error_type: str | None = None,
        validation_error_paths: list[str] | None = None,
        validation_error_codes: list[str] | None = None,
        repair_attempted: bool = False,
        repair_used: bool = False,
        expected_chat_count: int = 0,
        returned_chat_count: int = 0,
        missing_chat_count: int = 0,
        duplicate_chat_count: int = 0,
        unknown_chat_count: int = 0,
    ) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.validation_error_type = sanitize_validation_error_type(validation_error_type)
        self.validation_error_paths = sanitize_validation_paths(validation_error_paths or [])
        self.validation_error_codes = sanitize_validation_codes(validation_error_codes or [])
        self.repair_attempted = repair_attempted
        self.repair_used = repair_used
        self.expected_chat_count = expected_chat_count
        self.returned_chat_count = returned_chat_count
        self.missing_chat_count = missing_chat_count
        self.duplicate_chat_count = duplicate_chat_count
        self.unknown_chat_count = unknown_chat_count


@dataclass
class LLMValidationState:
    validation_error_type: str | None = None
    validation_error_paths: list[str] = field(default_factory=list)
    validation_error_codes: list[str] = field(default_factory=list)
    repair_attempted: bool = False
    repair_used: bool = False
    expected_chat_count: int = 0
    returned_chat_count: int = 0
    missing_chat_count: int = 0
    duplicate_chat_count: int = 0
    unknown_chat_count: int = 0

    def record(self, exc: Exception) -> None:
        error_type, paths, codes = _safe_validation_details(exc)
        self.validation_error_type = error_type
        self.validation_error_paths = paths
        self.validation_error_codes = codes


@dataclass
class LLMRawTrace:
    initial_output: str | None = None
    repair_output: str | None = None


class DigestChatCoverageError(ValueError):
    def __init__(self, message: str, *, path: str, code: str) -> None:
        super().__init__(message)
        self.path = path
        self.code = code


class DigestCountOnlySummaryError(ValueError):
    def __init__(self, message: str, *, path: str) -> None:
        super().__init__(message)
        self.path = path
        self.code = "count_only_summary"


def _safe_llm_error(exc: Exception) -> LLMError:
    return LLMError(
        f"llm_error:{exc.__class__.__name__}",
        reason_code="provider_error",
    )


def sanitize_validation_error_type(error_type: str | None) -> str | None:
    if error_type is None:
        return None
    return error_type if error_type in TRUSTED_VALIDATION_ERROR_TYPES else "ValidationError"


def sanitize_validation_codes(codes: list[str]) -> list[str]:
    return list(
        dict.fromkeys(
            code if code in TRUSTED_VALIDATION_ERROR_CODES else "validation_error"
            for code in codes
        )
    )


def _safe_path_component(component: object) -> str | int:
    if isinstance(component, int):
        return component
    value = str(component)
    return value if value in TRUSTED_DIGEST_SCHEMA_FIELDS else "<unknown_field>"


def _format_validation_path(location: tuple[object, ...]) -> str:
    path = ""
    for part in location:
        safe_part = _safe_path_component(part)
        if isinstance(safe_part, int):
            path += f"[{safe_part}]"
        elif path:
            path += f".{safe_part}"
        else:
            path = safe_part
    return path or "$"


def sanitize_validation_path(path: str) -> str:
    if path == "$":
        return path
    tokens = re.findall(r"\[\d+\]|[^.\[\]]+", str(path))
    if not tokens:
        return "$"
    sanitized = ""
    for token in tokens:
        if re.fullmatch(r"\[\d+\]", token):
            sanitized += token
            continue
        safe_token = token if token in TRUSTED_DIGEST_SCHEMA_FIELDS else "<unknown_field>"
        sanitized += ("." if sanitized else "") + safe_token
    return sanitized


def sanitize_validation_paths(paths: list[str]) -> list[str]:
    return list(dict.fromkeys(sanitize_validation_path(path) for path in paths))


def _safe_validation_details(exc: Exception) -> tuple[str, list[str], list[str]]:
    if isinstance(exc, ValidationError):
        errors = exc.errors(include_url=False, include_input=False)
        paths = list(
            dict.fromkeys(_format_validation_path(tuple(error.get("loc", ()))) for error in errors)
        )
        codes = list(
            dict.fromkeys(
                str(error.get("type") or "validation_error") for error in errors
            )
        )
        return (
            sanitize_validation_error_type(exc.__class__.__name__) or "ValidationError",
            sanitize_validation_paths(paths),
            sanitize_validation_codes(codes),
        )
    path = getattr(exc, "path", None)
    code = getattr(exc, "code", None)
    if path or code:
        return (
            sanitize_validation_error_type(exc.__class__.__name__) or "ValidationError",
            sanitize_validation_paths([path or "$"]),
            sanitize_validation_codes([code or "validation_error"]),
        )
    if isinstance(exc, json.JSONDecodeError):
        return "JSONDecodeError", ["$"], ["json_invalid"]
    if isinstance(exc, LLMError):
        return (
            sanitize_validation_error_type(
                exc.validation_error_type or exc.__class__.__name__
            )
            or "ValidationError",
            sanitize_validation_paths(exc.validation_error_paths or ["$"]),
            sanitize_validation_codes(exc.validation_error_codes or [exc.reason_code]),
        )
    return (
        sanitize_validation_error_type(exc.__class__.__name__) or "ValidationError",
        ["$"],
        ["validation_error"],
    )


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
            raise LLMError(
                "llm_error:InvalidJsonEnvelope",
                reason_code="invalid_json_envelope",
                validation_error_paths=["$"],
                validation_error_codes=["invalid_json_envelope"],
            )
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
        raise LLMError(
            "llm_error:InvalidJsonEnvelope",
            reason_code="invalid_json_envelope",
            validation_error_paths=["$"],
            validation_error_codes=["invalid_json_envelope"],
        )
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
        diagnostics: LLMValidationState | None = None,
        repair_context: str | None = None,
        observe_document: Callable[[dict], None] | None = None,
        raw_trace: LLMRawTrace | None = None,
    ) -> T:
        raw = self._json_completion(system, user, schema, prefer_schema=prefer_schema)
        if raw_trace is not None:
            raw_trace.initial_output = raw

        def parse(content: str) -> T:
            json_text = _extract_json_document(
                content,
                allow_code_fences=allow_code_fences,
            )
            normalized_json = _normalize_json_document(json_text)
            if observe_document is not None:
                document = json.loads(normalized_json)
                observe_document(document)
            result = schema.model_validate_json(normalized_json)
            return validate(result) if validate else result

        try:
            return parse(raw)
        except (LLMError, ValidationError, ValueError, json.JSONDecodeError) as first_error:
            state = diagnostics or LLMValidationState()
            state.record(first_error)
            state.repair_attempted = True
            logger.warning(
                "LLM JSON invalid; repair retry scheduled validation_error_type=%s "
                "validation_error_paths=%s validation_error_codes=%s repair_attempted=true "
                "repair_used=false",
                state.validation_error_type,
                ",".join(state.validation_error_paths) or "none",
                ",".join(state.validation_error_codes) or "none",
            )
            schema_json = json.dumps(schema.model_json_schema(), ensure_ascii=False)
            repair_user = (
                "Convert PREVIOUS_OUTPUT into valid JSON matching REQUIRED_SCHEMA exactly. "
                "Preserve the meaning, add every required field, and remove unknown fields. "
                "Return one JSON object only. No markdown, code fences, explanations, or prose.\n"
                f"REQUIRED_SCHEMA:\n{schema_json}\n"
                f"{repair_context + chr(10) if repair_context else ''}"
                "VALIDATION_ERROR_PATHS:\n"
                f"{json.dumps(state.validation_error_paths, ensure_ascii=True)}\n"
                "VALIDATION_ERROR_CODES:\n"
                f"{json.dumps(state.validation_error_codes, ensure_ascii=True)}\n"
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
                raise LLMError(
                    f"llm_error:{repair_error.__class__.__name__}",
                    reason_code="repair_failed",
                    validation_error_type=state.validation_error_type,
                    validation_error_paths=state.validation_error_paths,
                    validation_error_codes=state.validation_error_codes,
                    repair_attempted=True,
                    repair_used=False,
                    expected_chat_count=state.expected_chat_count,
                    returned_chat_count=state.returned_chat_count,
                    missing_chat_count=state.missing_chat_count,
                    duplicate_chat_count=state.duplicate_chat_count,
                    unknown_chat_count=state.unknown_chat_count,
                ) from repair_error
            if raw_trace is not None:
                raw_trace.repair_output = repaired
            try:
                result = parse(repaired)
                state.repair_used = True
                return result
            except (
                LLMError,
                ValidationError,
                ValueError,
                json.JSONDecodeError,
            ) as second_error:
                state.record(second_error)
                raise LLMError(
                    "LLM returned invalid JSON after repair retry",
                    reason_code="validation_failed",
                    validation_error_type=state.validation_error_type,
                    validation_error_paths=state.validation_error_paths,
                    validation_error_codes=state.validation_error_codes,
                    repair_attempted=True,
                    repair_used=False,
                    expected_chat_count=state.expected_chat_count,
                    returned_chat_count=state.returned_chat_count,
                    missing_chat_count=state.missing_chat_count,
                    duplicate_chat_count=state.duplicate_chat_count,
                    unknown_chat_count=state.unknown_chat_count,
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

    def daily_digest(
        self,
        payload: dict,
        *,
        raw_trace: LLMRawTrace | None = None,
    ) -> DailyDigest:
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

        validation_state = LLMValidationState(
            expected_chat_count=len(expected_chat_ids),
            missing_chat_count=len(expected_chat_ids),
        )

        def observe_document(document: dict) -> None:
            items = document.get("items")
            if not isinstance(items, list):
                validation_state.returned_chat_count = 0
                validation_state.missing_chat_count = len(expected_chat_ids)
                validation_state.duplicate_chat_count = 0
                validation_state.unknown_chat_count = 0
                return
            returned_ids: list[str] = []
            for item in items:
                if not isinstance(item, dict):
                    returned_ids.append("<invalid_chat_id>")
                    continue
                chat_id = item.get("chat_id")
                if isinstance(chat_id, bool) or not isinstance(chat_id, (str, int)):
                    returned_ids.append("<invalid_chat_id>")
                else:
                    returned_ids.append(str(chat_id).strip())
            returned_set = set(returned_ids)
            validation_state.returned_chat_count = len(items)
            validation_state.duplicate_chat_count = len(returned_ids) - len(returned_set)
            validation_state.missing_chat_count = len(expected_chat_ids - returned_set)
            validation_state.unknown_chat_count = len(returned_set - expected_chat_ids)

        def validate_response(response: DigestLLMResponse) -> DigestLLMResponse:
            actual_chat_id_list = [item.chat_id for item in response.items]
            actual_chat_ids = set(actual_chat_id_list)
            if len(actual_chat_id_list) != len(actual_chat_ids):
                raise DigestChatCoverageError(
                    "duplicate digest chat",
                    path="items",
                    code="duplicate_chat_id",
                )
            if expected_chat_ids - actual_chat_ids:
                raise DigestChatCoverageError(
                    "missing expected digest chat",
                    path="items",
                    code="missing_expected_chat",
                )
            if actual_chat_ids - expected_chat_ids:
                raise DigestChatCoverageError(
                    "unexpected digest chat",
                    path="items",
                    code="unexpected_chat_id",
                )
            for index, item in enumerate(response.items):
                if COUNT_ONLY_DIGEST_SUMMARY_RE.fullmatch(item.summary.strip()):
                    raise DigestCountOnlySummaryError(
                        "count-only digest summary",
                        path=f"items[{index}].summary",
                    )
            return response

        system = (
            "Create a concise practical Telegram digest in Russian. Return ONLY one valid JSON "
            "object with exactly this shape: {\"items\":[{\"chat_id\":\"1001\","
            "\"summary\":\"string\","
            "\"requests\":[\"string\"],\"context\":[\"string\"],"
            "\"actions\":[\"string\"],\"deadlines\":[\"string\"],"
            "\"open_telegram\":true,\"reason_to_open\":\"string\"}]}. "
            "Use exactly these eight fields and no others. All fields are required. Always use "
            "JSON arrays for requests, context, actions, and deadlines, even for one value; use "
            "[] when empty. open_telegram must be JSON true or false, never a quoted string or "
            "Russian yes/no. Copy each chat_id exactly from input. Create exactly one item per "
            "input chat and never merge chats. The summary must describe plans, questions, "
            "important context, and urgency—not a message count. Extract concrete response "
            "requests, confirmations, travel/planning context, and time phrases such as "
            "'завтра в 10'. For channel chats, summarize only concrete facts from the posts; "
            "never classify or describe the channel's genre, theme, or content category. "
            "reason_to_open must be specific to the conversation. "
            f"Reference timestamp: {reference_timestamp}. Timezone: {self.settings.timezone}. "
            "No markdown. No explanation. No ```json fences. No prose before or after JSON."
        )
        response = self._validated_json(
            DigestLLMResponse,
            system,
            json.dumps(payload, ensure_ascii=False),
            validate=validate_response,
            prefer_schema=True,
            allow_code_fences=False,
            diagnostics=validation_state,
            repair_context=(
                "EXPECTED_CHAT_IDS:\n"
                f"{json.dumps(sorted(expected_chat_ids), ensure_ascii=True)}"
            ),
            observe_document=observe_document,
            raw_trace=raw_trace,
        )
        digest = DailyDigest(date=str(payload.get("date") or ""))
        digest.diagnostics.validation_error_type = validation_state.validation_error_type
        digest.diagnostics.validation_error_paths = validation_state.validation_error_paths
        digest.diagnostics.validation_error_codes = validation_state.validation_error_codes
        digest.diagnostics.repair_attempted = validation_state.repair_attempted
        digest.diagnostics.repair_used = validation_state.repair_used
        digest.diagnostics.expected_chat_count = validation_state.expected_chat_count
        digest.diagnostics.returned_chat_count = validation_state.returned_chat_count
        digest.diagnostics.missing_chat_count = validation_state.missing_chat_count
        digest.diagnostics.duplicate_chat_count = validation_state.duplicate_chat_count
        digest.diagnostics.unknown_chat_count = validation_state.unknown_chat_count
        for item in response.items:
            chat = chats_by_id[item.chat_id]
            messages = chat.get("messages") if isinstance(chat.get("messages"), list) else []
            source_refs = [
                message["source_ref"]
                for message in messages
                if isinstance(message, dict) and isinstance(message.get("source_ref"), dict)
            ]
            requests = "; ".join(item.requests) or None
            context = "; ".join(item.context) or None
            actions = "; ".join(item.actions) or None
            deadlines = "; ".join(item.deadlines) or None
            common = {
                "chat": str(chat.get("chat_title") or "Telegram chat"),
                "summary": item.summary,
                "what_happened": item.summary,
                "requests_to_me": requests,
                "important_context": context,
                "action_items": actions,
                "should_open_telegram": item.open_telegram,
                "open_reason": (
                    None if item.reason_to_open == "Причина не указана." else item.reason_to_open
                ),
                "open_telegram": item.open_telegram,
                "deadline_text": deadlines,
                "source_refs": source_refs,
                "message_count": len(messages),
            }
            if normalized_chat_type(chat) == "private":
                digest.direct_messages.append(
                    DigestDirectMessage(
                        needs_reply=bool(item.requests or item.actions),
                        **common,
                    )
                )
            elif normalized_chat_type(chat) == "channel":
                digest.channel_updates.append(DigestGroupUpdate(**common))
            else:
                digest.group_updates.append(DigestGroupUpdate(**common))
        return digest
