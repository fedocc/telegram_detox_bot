from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.db.tables import MorningDigest

logger = logging.getLogger(__name__)
PROMPT_VERSION = "morning-v1"
TOKEN_THRESHOLD = 90_000
MAX_ITEMS = 6
SYSTEM_INSTRUCTION = """You are the editorial engine for a private Telegram information digest.

Your job is to compress messages from the user's explicitly selected Library sources
into an extremely short factual briefing.

PRIORITIZE:
1. deadlines, schedule changes, cancellations and required actions;
2. decisions, announcements and concrete changes;
3. information that materially changes what the user needs to know;
4. useful new materials, links or releases.

IGNORE:
- greetings, chatter and social filler;
- reactions and sticker-only messages;
- repeated or forwarded copies of the same information;
- service events unless they materially change the meaning;
- speculation with no concrete consequence;
- messages that add no new information.

RULES:
- Use only facts contained in the provided messages.
- Never invent details, dates, causes or links.
- Merge messages that describe the same event.
- Prefer one strong item over several weak items.
- Preserve uncertainty and attribution when the source is uncertain.
- If a deadline or exact time is provided, preserve it exactly.
- Write in concise natural Russian.
- Keep established technical names in English when that is clearer.
- Each item must be understandable without reading the source.
- Each item should normally be one sentence and at most two short sentences.
- Return at most 6 items.
- If fewer items are genuinely important, return fewer.
- If nothing is important, return an empty items array.
- source_refs may contain ONLY message refs present in the supplied context.
- Do not write Markdown.
- Do not add introductory or concluding filler."""
FINAL_TASK = "Return the single best morning briefing as strict JSON matching the schema."
FEW_SHOTS = (
    ({"messages": [
        {"ref": "m_a", "text": "Привет!"},
        {"ref": "m_b", "text": "Не забудьте сдать работу до 18:00 пятницы."},
        {"ref": "m_c", "text": "Спасибо"},
    ]}, {"title": "Главное к утру", "items": [{
        "category": "action", "title": "Дедлайн работы",
        "summary": "Работу нужно сдать до 18:00 пятницы.", "source_refs": ["m_b"],
    }]}),
    ({"messages": [
        {"ref": "m_d", "text": "Релиз перенесли на 12 сентября."},
        {"ref": "m_e", "text": "Подтверждение: релиз теперь 12 сентября."},
    ]}, {"title": "Главное к утру", "items": [{
        "category": "news", "title": "Перенос релиза",
        "summary": "Релиз перенесли на 12 сентября.", "source_refs": ["m_d", "m_e"],
    }]}),
)
NO_CONTENT = re.compile(
    r"^(?:привет|здравствуйте|доброе утро|добрый день|спасибо|ок|ага|👍|🙏)[!. ]*$",
    re.I,
)


class DigestItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    category: Literal["action", "study", "news", "other"]
    title: str = Field(min_length=1, max_length=160)
    summary: str = Field(min_length=1, max_length=500)
    source_refs: list[str] = Field(min_length=1, max_length=20)


class DigestPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=160)
    items: list[DigestItem] = Field(max_length=MAX_ITEMS)


class Provider(Protocol):
    def generate(self, context: list[dict], *, candidate_mode: bool = False) -> DigestPayload: ...


def stable_ref(source_id: str, message_id: int) -> str:
    value = hashlib.sha256(f"{source_id}:{message_id}".encode()).hexdigest()[:20]
    return f"m_{value}"


def preprocess(rows: list[dict]) -> tuple[list[dict], dict[str, dict]]:
    output, refs, seen_text, seen_forward = [], {}, {}, {}
    for row in sorted(
        rows, key=lambda item: (item["timestamp"], item["source_id"], item["message_id"]),
    ):
        text = " ".join(str(row.get("text") or row.get("caption") or "").split()).strip()
        if not text or row.get("service") or row.get("sticker_only") or NO_CONTENT.fullmatch(text):
            continue
        normalized = text.casefold()
        forwarded = row.get("forward_identity")
        ref = stable_ref(row["source_id"], int(row["message_id"]))
        refs[ref] = {"source_id": row["source_id"], "message_id": int(row["message_id"])}
        duplicate_at = seen_text.get(normalized)
        if duplicate_at is None and forwarded:
            duplicate_at = seen_forward.get(forwarded)
        if duplicate_at is not None:
            output[duplicate_at]["refs"].append(ref)
            continue
        seen_text[normalized] = len(output)
        if forwarded:
            seen_forward[forwarded] = len(output)
        item = {"ref": ref, "timestamp": row["timestamp"], "source": row["source_title"],
                "text": text, "refs": [ref]}
        if row.get("sender"):
            item["sender"] = row["sender"]
        if row.get("reply_context"):
            item["reply_context"] = row["reply_context"]
        output.append(item)
    return output, refs


def approximate_tokens(context: list[dict]) -> int:
    return max(1, len(json.dumps(context, ensure_ascii=False)) // 4)


def validate_refs(payload: DigestPayload, allowed: set[str]) -> DigestPayload:
    items = []
    for item in payload.items[:MAX_ITEMS]:
        refs = list(dict.fromkeys(ref for ref in item.source_refs if ref in allowed))
        if refs:
            items.append(item.model_copy(update={"source_refs": refs}))
    return payload.model_copy(update={"items": items})


class GeminiDigestProvider:
    ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

    def __init__(self, settings, *, sleeper=time.sleep, opener=urlopen):
        self.model = settings.digest_model
        self.api_key = settings.gemini_api_key
        self.sleeper = sleeper
        self.opener = opener

    def generate(self, context: list[dict], *, candidate_mode: bool = False) -> DigestPayload:
        instruction = FINAL_TASK
        if candidate_mode:
            instruction = (
                "Return factual candidate items only; preserve every supporting source ref."
            )
        user = json.dumps(context, ensure_ascii=False, separators=(",", ":")) + "\n\n" + instruction
        if not self.api_key:
            raise RuntimeError("Gemini API not configured")
        contents = []
        for example_input, example_output in FEW_SHOTS:
            contents.extend([
                {"role": "user", "parts": [{"text": json.dumps(
                    example_input, ensure_ascii=False,
                )}]},
                {"role": "model", "parts": [{"text": json.dumps(
                    example_output, ensure_ascii=False,
                )}]},
            ])
        contents.append({"role": "user", "parts": [{"text": user}]})
        body = json.dumps({
            "systemInstruction": {"parts": [{"text": SYSTEM_INSTRUCTION}]},
            "contents": contents,
            "generationConfig": {
                "temperature": 0,
                "thinkingConfig": {"thinkingLevel": "low"},
                "responseFormat": {"text": {
                    "mimeType": "application/json",
                    "schema": DigestPayload.model_json_schema(),
                }},
            },
        }, ensure_ascii=False).encode()
        request = Request(  # noqa: S310 - endpoint is a fixed HTTPS Gemini API URL.
            self.ENDPOINT.format(model=self.model), data=body, method="POST",
            headers={"Content-Type": "application/json", "x-goog-api-key": self.api_key},
        )
        last_error = None
        for attempt in range(3):
            try:
                with self.opener(request, timeout=120) as response:
                    envelope = json.load(response)
                parts = envelope["candidates"][0]["content"]["parts"]
                text = "".join(part.get("text", "") for part in parts)
                return DigestPayload.model_validate_json(text)
            except HTTPError as exc:
                last_error = exc
                if exc.code in {429, 500, 502, 503, 504} and attempt < 2:
                    self.sleeper(2 ** attempt)
                    continue
                raise RuntimeError(f"Gemini API request failed (HTTP {exc.code})") from None
            except (URLError, TimeoutError) as exc:
                last_error = exc
                if attempt < 2:
                    self.sleeper(2 ** attempt)
                    continue
                raise RuntimeError("Gemini API request failed (network)") from None
            except (KeyError, IndexError, json.JSONDecodeError, ValueError) as exc:
                raise RuntimeError(
                    f"Morning digest generation failed ({type(exc).__name__})"
                ) from None
        raise RuntimeError("Gemini API retry limit reached") from last_error


def generate_payload(provider: Provider, context: list[dict]) -> DigestPayload:
    if not context:
        return DigestPayload(title="Утро", items=[])
    if approximate_tokens(context) <= TOKEN_THRESHOLD:
        return provider.generate(context)
    candidates = []
    for start in range(0, len(context), 250):
        chunk = context[start:start + 250]
        partial = validate_refs(provider.generate(chunk, candidate_mode=True),
                                {row["ref"] for row in chunk})
        candidates.extend(item.model_dump() for item in partial.items)
    return provider.generate(candidates)


async def run_digest(
    service, factory, settings, cutoff: datetime, provider: Provider | None = None,
):
    cutoff = cutoff.astimezone(UTC)
    with factory() as session:
        existing = session.scalar(select(MorningDigest).where(
            MorningDigest.period_end == cutoff.replace(tzinfo=None),
            MorningDigest.status == "success",
        ))
        if existing:
            return existing
        previous = session.scalar(select(MorningDigest).where(
            MorningDigest.status == "success", MorningDigest.period_end <= cutoff,
        ).order_by(MorningDigest.period_end.desc()))
        start = (
            previous.period_end.replace(tzinfo=UTC)
            if previous else cutoff - timedelta(hours=24)
        )
    rows = await service.digest_rows(start, cutoff)
    context, refs = preprocess(rows)
    engine = provider or GeminiDigestProvider(settings)
    payload = await asyncio.to_thread(generate_payload, engine, context)
    payload = validate_refs(payload, set(refs))
    stored = {"title": payload.title, "items": []}
    for item in payload.items:
        value = item.model_dump()
        value["sources"] = [refs[ref] for ref in item.source_refs]
        stored["items"].append(value)
    with factory() as session:
        duplicate = session.scalar(select(MorningDigest).where(
            MorningDigest.period_start == start.replace(tzinfo=None),
            MorningDigest.period_end == cutoff.replace(tzinfo=None),
            MorningDigest.status == "success",
        ))
        if duplicate:
            return duplicate
        record = MorningDigest(period_start=start.replace(tzinfo=None),
            period_end=cutoff.replace(tzinfo=None),
            generated_at=datetime.now(UTC).replace(tzinfo=None),
            model=settings.digest_model, prompt_version=PROMPT_VERSION,
            payload_json=json.dumps(stored, ensure_ascii=False), status="success")
        session.add(record)
        session.commit()
        session.refresh(record)
        return record


def latest_digest(factory):
    with factory() as session:
        row = session.scalar(select(MorningDigest).where(MorningDigest.status == "success")
                             .order_by(MorningDigest.period_end.desc()))
        if row is None:
            return None
        payload = json.loads(row.payload_json)
        for item in payload.get("items", []):
            sources = item.pop("sources", [])
            item["links"] = [f"/?library={source['source_id']}&message={source['message_id']}"
                             for source in sources]
        return {"period_end": row.period_end.isoformat(), **payload}
