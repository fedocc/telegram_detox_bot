from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import logging
import re
import time
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.db.tables import MorningDigest

logger = logging.getLogger(__name__)
PROMPT_VERSION = "morning-v1"
TOKEN_THRESHOLD = 90_000
MAX_ITEMS = 6
SYSTEM_INSTRUCTION = """You are the editorial engine for a private Telegram information digest.

Your job is to compress messages from the user's permitted information sources
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
URL = re.compile(r"\bhttps?://[^\s<>()]+", re.I)
PRIVATE_SCHEME = re.compile(
    r"\b(?:vmess|vless|trojan|ss|ssr|socks5?|wg|wireguard|openvpn|otpauth|"
    r"postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqps?)://[^\s]+", re.I,
)
BEARER = re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{8,}", re.I)
NAMED_SECRET = re.compile(
    r"\b(?:api[_ -]?key|access[_ -]?token|refresh[_ -]?token|auth(?:orization)?|"
    r"password|passwd|session|secret|private[_ -]?key|preshared[_ -]?key|"
    r"client[_ -]?secret|auth[_ -]?code|otp)\b\s*[:=]\s*[^\s,;]+",
    re.I,
)
EMAIL = re.compile(r"(?<![\w.+-])[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}(?![\w.-])")
PHONE = re.compile(r"(?<!\w)(?:\+?\d[\d ()-]{8,}\d)(?!\w)")
USERNAME = re.compile(r"(?<!\w)@[A-Za-z][A-Za-z0-9_]{4,31}\b")
NUMERIC_ID = re.compile(r"(?<!\w)-?\d{7,}(?!\w)")
LONG_CREDENTIAL = re.compile(
    r"(?<![\w-])(?=[A-Za-z0-9_./+=-]{24,}(?![\w-]))"
    r"(?=[^\s]*[A-Za-z])(?=[^\s]*\d)[A-Za-z0-9_./+=-]+"
)


def redact_digest_text(value: str) -> tuple[str, int]:
    """Deterministically minimize secrets and personal identifiers before Gemini."""
    text = str(value or "")
    redactions = 0

    def replace(pattern, replacement):
        nonlocal text, redactions
        text, count = pattern.subn(replacement, text)
        redactions += count

    replace(PRIVATE_SCHEME, "[private link]")
    replace(BEARER, "Bearer [redacted]")

    def safe_url(match):
        nonlocal redactions
        redactions += 1
        try:
            host = urlsplit(match.group(0)).hostname
        except ValueError:
            host = None
        if host:
            try:
                ipaddress.ip_address(host)
            except ValueError:
                pass
            else:
                host = None
        return f"[link: {host.lower()}]" if host else "[link]"

    text = URL.sub(safe_url, text)
    replace(NAMED_SECRET, "[credential redacted]")
    replace(EMAIL, "[email]")
    replace(PHONE, "[phone]")
    replace(USERNAME, "[username]")
    replace(NUMERIC_ID, "[id]")
    replace(LONG_CREDENTIAL, "[credential redacted]")
    return " ".join(text.split()).strip(), redactions


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


class GeminiProviderError(RuntimeError):
    def __init__(self, message: str, *, transient: bool):
        super().__init__(message)
        self.transient = transient


def stable_ref(source_id: str, message_id: int) -> str:
    value = hashlib.sha256(f"{source_id}:{message_id}".encode()).hexdigest()[:20]
    return f"m_{value}"


def preprocess(
    rows: list[dict], *, report: dict[str, int] | None = None,
) -> tuple[list[dict], dict[str, dict]]:
    output, refs, seen_text, seen_forward = [], {}, {}, {}
    redaction_count = 0
    for row in sorted(
        rows, key=lambda item: (item["timestamp"], item["source_id"], item["message_id"]),
    ):
        text, count = redact_digest_text(row.get("text") or row.get("caption") or "")
        redaction_count += count
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
        output.append(item)
    if report is not None:
        report["redactions"] = redaction_count
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
    # SDK examples use application/json, but this v1beta REST enum requires APPLICATION_JSON.
    JSON_MIME_TYPE = "APPLICATION_JSON"

    def __init__(self, settings, *, sleeper=time.sleep, opener=urlopen):
        self.model = settings.digest_model
        self.api_key = settings.gemini_api_key
        self.sleeper = sleeper
        self.opener = opener

    @staticmethod
    def _response_text(envelope: dict) -> str:
        parts = envelope["candidates"][0]["content"]["parts"]
        return "".join(part.get("text", "") for part in parts)

    def _safe_http_error(self, exc: HTTPError, sensitive: tuple[str, ...]) -> str:
        code, status, message = exc.code, "UNKNOWN", "Request rejected"
        try:
            envelope = json.loads(exc.read(65_536))
            error = envelope.get("error", {}) if isinstance(envelope, dict) else {}
            code = int(error.get("code", code))
            candidate_status = str(error.get("status", status))
            if re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", candidate_status):
                status = candidate_status
            message = str(error.get("message", message))
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            pass
        for value in (self.api_key, *sensitive):
            if value:
                message = message.replace(value, "[redacted]")
        message = " ".join(message.split())[:1000] or "Request rejected"
        return f"Gemini API request failed: HTTP {code} {status}: {message}"

    def _post(self, payload: dict, *, sensitive: tuple[str, ...] = ()) -> dict:
        if not self.api_key:
            raise GeminiProviderError("Gemini API not configured", transient=False)
        request = Request(  # noqa: S310 - endpoint is a fixed HTTPS Gemini API URL.
            self.ENDPOINT.format(model=self.model),
            data=json.dumps(payload, ensure_ascii=False).encode(),
            method="POST",
            headers={"Content-Type": "application/json", "x-goog-api-key": self.api_key},
        )
        last_error = None
        for attempt in range(3):
            try:
                with self.opener(request, timeout=120) as response:
                    return json.load(response)
            except HTTPError as exc:
                last_error = exc
                if exc.code in {429, 500, 502, 503, 504} and attempt < 2:
                    self.sleeper(2 ** attempt)
                    continue
                raise GeminiProviderError(
                    self._safe_http_error(exc, sensitive),
                    transient=exc.code in {429, 500, 502, 503, 504},
                ) from None
            except (URLError, TimeoutError) as exc:
                last_error = exc
                if attempt < 2:
                    self.sleeper(2 ** attempt)
                    continue
                raise GeminiProviderError(
                    "Gemini API request failed (network)", transient=True,
                ) from None
        raise GeminiProviderError(
            "Gemini API retry limit reached", transient=True,
        ) from last_error

    def smoke_text(self) -> str:
        prompt = "Return OK"
        envelope = self._post({
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"thinkingConfig": {"thinkingLevel": "low"}},
        }, sensitive=(prompt,))
        try:
            result = self._response_text(envelope).strip()
        except (KeyError, IndexError, TypeError):
            raise RuntimeError("Gemini API smoke response was malformed") from None
        if not result:
            raise RuntimeError("Gemini API smoke response was empty")
        return result

    def smoke_structured(self) -> dict:
        prompt = "Return an object whose status is OK."
        schema = {
            "type": "object",
            "additionalProperties": False,
            "properties": {"status": {"type": "string", "enum": ["OK"]}},
            "required": ["status"],
        }
        envelope = self._post({
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "thinkingConfig": {"thinkingLevel": "low"},
                "responseFormat": {"text": {
                    "mimeType": self.JSON_MIME_TYPE, "schema": schema,
                }},
            },
        }, sensitive=(prompt,))
        try:
            result = json.loads(self._response_text(envelope))
        except (KeyError, IndexError, TypeError, json.JSONDecodeError):
            raise RuntimeError("Gemini structured-output smoke response was malformed") from None
        if result != {"status": "OK"}:
            raise RuntimeError("Gemini structured-output smoke response failed validation")
        return result

    def generate(self, context: list[dict], *, candidate_mode: bool = False) -> DigestPayload:
        instruction = FINAL_TASK
        if candidate_mode:
            instruction = (
                "Return factual candidate items only; preserve every supporting source ref."
            )
        user = json.dumps(context, ensure_ascii=False, separators=(",", ":")) + "\n\n" + instruction
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
        body = {
            "systemInstruction": {"parts": [{"text": SYSTEM_INSTRUCTION}]},
            "contents": contents,
            "generationConfig": {
                "thinkingConfig": {"thinkingLevel": "low"},
                "responseFormat": {"text": {
                    "mimeType": self.JSON_MIME_TYPE,
                    "schema": DigestPayload.model_json_schema(),
                }},
            },
        }
        try:
            envelope = self._post(body, sensitive=(user, SYSTEM_INSTRUCTION))
            return DigestPayload.model_validate_json(self._response_text(envelope))
        except (KeyError, IndexError, TypeError, json.JSONDecodeError, ValueError) as exc:
            raise RuntimeError(
                f"Morning digest generation failed ({type(exc).__name__})"
            ) from None


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
    report = getattr(service, "last_digest_report", None)
    context, refs = preprocess(rows, report=report if isinstance(report, dict) else None)
    if isinstance(report, dict):
        logger.info(
            "morning_digest scope total=%d groups=%d channels=%d bots=%d "
            "human_dms_excluded=%d ignored=%d unavailable=%d redactions=%d",
            report.get("total_dialogs", 0), report.get("included_groups", 0),
            report.get("included_channels", 0), report.get("included_bots", 0),
            report.get("excluded_human_dms", 0), report.get("ignored", 0),
            report.get("unavailable", 0), report.get("redactions", 0),
        )
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


def desired_digest_cutoff(now: datetime, digest_time: str) -> datetime | None:
    """Return today's canonical cutoff once it is due.

    Before the configured time reconciliation waits for today's run instead of
    creating an old first-run digest for yesterday.
    """
    hour, minute = (int(part) for part in digest_time.split(":", 1))
    cutoff = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return cutoff if now >= cutoff else None


def digest_succeeded(factory, cutoff: datetime) -> bool:
    cutoff = cutoff.astimezone(UTC).replace(tzinfo=None)
    with factory() as session:
        return session.scalar(select(MorningDigest.id).where(
            MorningDigest.period_end == cutoff,
            MorningDigest.status == "success",
        )) is not None


class MorningDigestReconciler:
    """Single-process, idempotent delivery for the daily canonical cutoff."""

    def __init__(self, service, factory, settings, *, provider=None):
        self.service = service
        self.factory = factory
        self.settings = settings
        self.provider = provider
        self.lock = asyncio.Lock()
        self.permanent_failures: set[datetime] = set()

    async def reconcile(
        self, *, now: datetime | None = None, cutoff: datetime | None = None,
    ) -> str:
        zone_now = now or datetime.now(ZoneInfo(self.settings.timezone))
        cutoff = cutoff or desired_digest_cutoff(zone_now, self.settings.digest_time)
        if cutoff is None:
            return "not_due"
        cutoff = cutoff.replace(second=0, microsecond=0)
        key = cutoff.astimezone(UTC)
        if key in self.permanent_failures:
            return "blocked"
        async with self.lock:
            if digest_succeeded(self.factory, cutoff):
                return "complete"
            if key in self.permanent_failures:
                return "blocked"
            try:
                await run_digest(
                    self.service, self.factory, self.settings, cutoff, self.provider,
                )
            except GeminiProviderError as exc:
                if exc.transient:
                    logger.warning(
                        "morning_digest pending retry cutoff=%s "
                        "next retry through reconciliation: %s",
                        cutoff.isoformat(), exc,
                    )
                    return "pending"
                self.permanent_failures.add(key)
                logger.error(
                    "morning_digest blocked cutoff=%s permanent provider error: %s",
                    cutoff.isoformat(), exc,
                )
                return "blocked"
            except Exception as exc:
                logger.error(
                    "morning_digest pending retry cutoff=%s "
                    "next retry through reconciliation: %s",
                    cutoff.isoformat(), type(exc).__name__,
                )
                return "pending"
            logger.info("morning_digest complete cutoff=%s", cutoff.isoformat())
            return "complete"


def _digest_json(row):
    payload = json.loads(row.payload_json)
    for item in payload.get("items", []):
        sources = item.pop("sources", [])
        item["links"] = [f"/?library={source['source_id']}&message={source['message_id']}"
                         for source in sources]
    return {"period_end": row.period_end.isoformat(), **payload}


def latest_digest(factory):
    with factory() as session:
        row = session.scalar(select(MorningDigest).where(MorningDigest.status == "success")
                             .order_by(MorningDigest.period_end.desc()))
        return _digest_json(row) if row is not None else None


def digest_history(factory, timezone, *, now=None):
    zone = ZoneInfo(timezone)
    local_now = now or datetime.now(zone)
    if local_now.tzinfo is None:
        local_now = local_now.replace(tzinfo=zone)
    first_day = local_now.astimezone(zone).date() - timedelta(days=6)
    start = datetime.combine(first_day, datetime.min.time(), zone).astimezone(UTC)
    end = datetime.combine(
        local_now.astimezone(zone).date() + timedelta(days=1), datetime.min.time(), zone,
    ).astimezone(UTC)
    with factory() as session:
        rows = list(session.scalars(select(MorningDigest).where(
            MorningDigest.status == "success",
            MorningDigest.period_end >= start.replace(tzinfo=None),
            MorningDigest.period_end < end.replace(tzinfo=None),
        ).order_by(MorningDigest.period_end.desc())))
    result = []
    seen_days = set()
    for row in rows:
        local_day = row.period_end.replace(tzinfo=UTC).astimezone(zone).date()
        if local_day in seen_days:
            continue
        seen_days.add(local_day)
        result.append(_digest_json(row))
        if len(result) == 7:
            break
    return result
