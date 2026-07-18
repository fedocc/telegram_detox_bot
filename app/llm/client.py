from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import TypeVar

from openai import OpenAI, OpenAIError
from pydantic import BaseModel, TypeAdapter, ValidationError

from app.config import Settings
from app.models.schemas import DailyDigest, P0Decision

T = TypeVar("T", bound=BaseModel)
logger = logging.getLogger(__name__)


class LLMError(RuntimeError):
    pass


def _safe_llm_error(exc: Exception) -> LLMError:
    return LLMError(f"llm_error:{exc.__class__.__name__}")


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


def _extract_json_document(content: str) -> str:
    text = content.strip()
    if text.startswith("```"):
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

    def _validated_json(self, schema: type[T], system: str, user: str) -> T:
        raw = self._json_completion(system, user, schema)
        json_text = _extract_json_document(raw)
        try:
            return schema.model_validate_json(_normalize_json_document(json_text))
        except (ValidationError, ValueError, json.JSONDecodeError) as first_error:
            logger.warning(
                "LLM JSON invalid; trying one repair retry: %s",
                first_error.__class__.__name__,
            )
            repair_user = (
                "Repair the following response into valid JSON matching the requested schema. "
                "Return only valid JSON. No markdown. No code fences. No explanation.\n\n"
                f"{raw}"
            )
            try:
                repaired = self._json_completion(system, repair_user, schema, prefer_schema=False)
            except Exception as repair_error:
                raise _safe_llm_error(repair_error) from repair_error
            repaired_json = _extract_json_document(repaired)
            try:
                return schema.model_validate_json(_normalize_json_document(repaired_json))
            except (ValidationError, ValueError, json.JSONDecodeError) as second_error:
                raise LLMError("LLM returned invalid JSON after repair retry") from second_error

    def classify_p0(self, payload: dict) -> P0Decision:
        message = payload.get("message", {}) if isinstance(payload, dict) else {}
        reference_timestamp = message.get("timestamp") or "unknown"
        system = (
            "Lightweight Telegram P0 classifier. Return JSON only with keys: "
            "status, summary, reason, action, deadline_text, deadline_at, confidence. status must "
            "be P0_STRICT, P0_CANDIDATE, or NOT_P0. Apply detox routing by chat type. For an "
            "incoming private chat, P0_STRICT is appropriate for a clear response, action, call, "
            "or check request, or urgent/time-sensitive wording; ordinary conversation is not "
            "P0. Time words such as today or now do not create a request by themselves. For "
            "groups, use P0_STRICT for a direct mention, a reply to the user with a "
            "request/urgency, an explicit deadline plus request/urgency, or a watchlist match plus "
            "request/urgency. Unrouted group urgency is not P0_STRICT. Read authoritative routing "
            "facts from message.policy and trusted status from message.trusted_sender. A trusted "
            "sender can lower uncertainty for a private request but never makes ordinary chat P0. "
            "If uncertain, use P0_CANDIDATE. "
            f"Reference timestamp: {reference_timestamp}. Timezone: {self.settings.timezone}. "
            "deadline_at must be exact ISO 8601 datetime or null. "
            "Put relative or human deadline wording only into deadline_text. "
            "Return only valid JSON. No markdown. No code fences. No explanation."
        )
        return self._validated_json(P0Decision, system, json.dumps(payload, ensure_ascii=False))

    def daily_digest(self, payload: dict) -> DailyDigest:
        reference_timestamp = payload.get("reference_timestamp") or payload.get("date") or "unknown"
        system = (
            "Create a short practical Telegram daily digest as strict JSON. "
            "Never hide direct messages. If unsure whether something is safe "
            "background, put it in review. "
            "Produce one semantic item per private chat and one per relevant group chat. "
            "For every direct_messages and group_updates item, include all required keys: "
            "summary, what_happened, requests_to_me, important_context, action_items, "
            "should_open_telegram, and open_reason. Use null for an unknown text field, but "
            "always include the key. Do not output counts as the summary and do "
            "not restate each message. Media is already collapsed by the application. "
            "Keep output concise and action-oriented. "
            f"Reference timestamp: {reference_timestamp}. Timezone: {self.settings.timezone}. "
            "Use deadline_text for relative or human deadline wording. "
            "Use deadline_at only for exact ISO 8601 datetimes, otherwise null. "
            "Return only valid JSON. No markdown. No code fences. No explanation."
        )
        digest = self._validated_json(DailyDigest, system, json.dumps(payload, ensure_ascii=False))
        required = {
            "summary",
            "what_happened",
            "requests_to_me",
            "important_context",
            "action_items",
            "should_open_telegram",
            "open_reason",
        }
        for item in [*digest.direct_messages, *digest.group_updates]:
            if not required.issubset(item.model_fields_set):
                raise LLMError("LLM daily digest omitted required semantic fields")
        return digest
