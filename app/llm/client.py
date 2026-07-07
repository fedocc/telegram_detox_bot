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
        try:
            response = self.client.chat.completions.create(
                model=self.settings.aitunnel_model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                response_format=response_format,
                temperature=0,
            )
        except OpenAIError:
            if not prefer_schema:
                raise
            response = self.client.chat.completions.create(
                model=self.settings.aitunnel_model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                response_format={"type": "json_object"},
                temperature=0,
            )
        content = response.choices[0].message.content
        if not content:
            raise LLMError("empty LLM response")
        return content

    def _validated_json(self, schema: type[T], system: str, user: str) -> T:
        raw = self._json_completion(system, user, schema)
        try:
            return schema.model_validate_json(raw)
        except (ValidationError, ValueError, json.JSONDecodeError) as first_error:
            logger.warning("LLM JSON invalid; trying one repair retry: %s", first_error)
            repair_user = (
                "Repair the following response into valid JSON matching the requested schema. "
                "Return JSON only, no markdown.\n\n"
                f"{raw}"
            )
            repaired = self._json_completion(system, repair_user, schema, prefer_schema=False)
            try:
                return schema.model_validate_json(repaired)
            except (ValidationError, ValueError, json.JSONDecodeError) as second_error:
                raise LLMError("LLM returned invalid JSON after repair retry") from second_error

    def classify_p0(self, payload: dict) -> P0Decision:
        system = (
            "Lightweight Telegram P0 classifier. Return JSON only with keys: "
            "status, summary, action, deadline, confidence. status must be P0, "
            "NOT_P0, or REVIEW. Be conservative: if uncertain, use REVIEW. "
            "P0 means same-day urgent action or personal/family risk."
        )
        return self._validated_json(P0Decision, system, json.dumps(payload, ensure_ascii=False))

    def daily_digest(self, payload: dict) -> DailyDigest:
        system = (
            "Create a short practical Telegram daily digest as strict JSON. "
            "Never hide direct messages. If unsure whether something is safe "
            "background, put it in review. "
            "Mention unprocessed media in review. Keep output concise and action-oriented."
        )
        return self._validated_json(DailyDigest, system, json.dumps(payload, ensure_ascii=False))
