from __future__ import annotations

import json
import logging
from typing import TypeVar

from openai import OpenAI, OpenAIError
from pydantic import BaseModel, ValidationError

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
        prefer_schema: bool = True,
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
            return schema.model_validate_json(json_text)
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
                return schema.model_validate_json(repaired_json)
            except (ValidationError, ValueError, json.JSONDecodeError) as second_error:
                raise LLMError("LLM returned invalid JSON after repair retry") from second_error

    def classify_p0(self, payload: dict) -> P0Decision:
        system = (
            "Lightweight Telegram P0 classifier. Return JSON only with keys: "
            "status, summary, action, deadline, confidence. status must be P0, "
            "NOT_P0, or REVIEW. Be conservative: if uncertain, use REVIEW. "
            "P0 means same-day urgent action or personal/family risk. "
            "Return only valid JSON. No markdown. No code fences. No explanation."
        )
        return self._validated_json(P0Decision, system, json.dumps(payload, ensure_ascii=False))

    def daily_digest(self, payload: dict) -> DailyDigest:
        system = (
            "Create a short practical Telegram daily digest as strict JSON. "
            "Never hide direct messages. If unsure whether something is safe "
            "background, put it in review. "
            "Mention unprocessed media in review. Keep output concise and action-oriented. "
            "Return only valid JSON. No markdown. No code fences. No explanation."
        )
        return self._validated_json(DailyDigest, system, json.dumps(payload, ensure_ascii=False))
