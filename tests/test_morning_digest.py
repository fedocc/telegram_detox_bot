from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime, timedelta
from io import BytesIO
from urllib.error import HTTPError
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from app.config import Settings
from app.db.session import init_db
from app.db.tables import MorningDigest
from app.inbox.digest import (
    DigestItem,
    DigestPayload,
    GeminiDigestProvider,
    GeminiProviderError,
    MorningDigestReconciler,
    desired_digest_cutoff,
    latest_digest,
    preprocess,
    run_digest,
    stable_ref,
    validate_refs,
)


class Provider:
    def __init__(self, fail=False):
        self.fail = fail
        self.calls = []

    def generate(self, context, *, candidate_mode=False):
        self.calls.append((context, candidate_mode))
        if self.fail:
            raise RuntimeError("provider failed")
        refs = [row["ref"] for row in context if "ref" in row]
        return DigestPayload(title="Главное", items=[] if not refs else [DigestItem(
            category="action", title="Дедлайн", summary="Сдать работу до 18:00.",
            source_refs=[refs[0], "m_unknown"],
        )])


class SequenceProvider(Provider):
    def __init__(self, failures=0, *, transient=True, delay=0):
        super().__init__()
        self.failures = failures
        self.transient = transient
        self.delay = delay

    def generate(self, context, *, candidate_mode=False):
        if self.delay:
            time.sleep(self.delay)
        self.calls.append((context, candidate_mode))
        if len(self.calls) <= self.failures:
            raise GeminiProviderError("sanitized provider failure", transient=self.transient)
        refs = [item["ref"] for item in context]
        return DigestPayload(title="Главное", items=[DigestItem(
            category="news", title="Новость", summary="Факт.", source_refs=refs[:1],
        )])


class Service:
    def __init__(self, rows):
        self.rows = rows
        self.windows = []

    async def digest_rows(self, start, end):
        self.windows.append((start, end))
        return self.rows


def row(message_id=1, text="Сдать работу до 18:00", source="course"):
    return {"source_id": source, "source_title": "Course", "message_id": message_id,
            "timestamp": "2026-09-18T09:00:00+00:00", "sender": "Teacher", "text": text}


def test_preprocess_deduplicates_noise_and_keeps_stable_opaque_refs():
    context, refs = preprocess([row(), row(2), row(3, "Привет"), row(4, "Новая тема")])
    assert [item["text"] for item in context] == ["Сдать работу до 18:00", "Новая тема"]
    assert context[0]["ref"] == stable_ref("course", 1)
    assert context[0]["refs"] == [stable_ref("course", 1), stable_ref("course", 2)]
    assert refs[context[0]["ref"]] == {"source_id": "course", "message_id": 1}


def test_unknown_refs_are_removed_and_empty_items_rejected():
    payload = DigestPayload(title="Утро", items=[
        DigestItem(category="news", title="A", summary="B", source_refs=["m_ok", "m_bad"]),
        DigestItem(category="other", title="C", summary="D", source_refs=["m_bad"]),
    ])
    result = validate_refs(payload, {"m_ok"})
    assert len(result.items) == 1 and result.items[0].source_refs == ["m_ok"]


async def test_window_idempotency_failed_cutoff_and_deep_link(settings):
    factory = init_db(settings)
    cutoff = datetime(2026, 9, 19, 4, tzinfo=UTC)
    service = Service([row()])
    provider = Provider()
    first = await run_digest(service, factory, settings, cutoff, provider)
    again = await run_digest(service, factory, settings, cutoff, provider)
    assert first.id == again.id and len(provider.calls) == 1
    assert service.windows[0] == (cutoff - timedelta(hours=24), cutoff)
    result = latest_digest(factory)
    assert result["items"][0]["links"] == ["/?library=course&message=1"]

    later = cutoff + timedelta(days=1)
    failing = Provider(fail=True)
    try:
        await run_digest(service, factory, settings, later, failing)
    except RuntimeError:
        pass
    provider2 = Provider()
    await run_digest(service, factory, settings, later, provider2)
    assert service.windows[-1][0] == cutoff


async def test_reconciliation_retries_transient_failure_at_same_canonical_cutoff(settings):
    factory = init_db(settings)
    service = Service([row()])
    provider = SequenceProvider(failures=1)
    reconciler = MorningDigestReconciler(service, factory, settings, provider=provider)
    zone = ZoneInfo(settings.timezone)
    scheduled = datetime(2026, 9, 20, 7, 0, tzinfo=zone)

    assert await reconciler.reconcile(now=scheduled) == "pending"
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(MorningDigest)) == 0
    assert await reconciler.reconcile(now=scheduled + timedelta(minutes=15)) == "complete"
    with factory() as session:
        stored = session.scalar(select(MorningDigest))
        assert stored.period_end == scheduled.astimezone(UTC).replace(tzinfo=None)
    expected = (scheduled.astimezone(UTC) - timedelta(hours=24), scheduled.astimezone(UTC))
    assert service.windows == [expected, expected]
    assert len(provider.calls) == 2
    assert await reconciler.reconcile(now=scheduled + timedelta(hours=1)) == "complete"
    assert len(provider.calls) == 2


async def test_reconciliation_startup_before_and_after_cutoff(settings):
    factory = init_db(settings)
    service = Service([row()])
    provider = Provider()
    reconciler = MorningDigestReconciler(service, factory, settings, provider=provider)
    zone = ZoneInfo(settings.timezone)
    before = datetime(2026, 9, 20, 6, 59, tzinfo=zone)
    after = datetime(2026, 9, 20, 8, 30, tzinfo=zone)

    assert desired_digest_cutoff(before, settings.digest_time) is None
    assert await reconciler.reconcile(now=before) == "not_due"
    assert not provider.calls
    assert await reconciler.reconcile(now=after) == "complete"
    assert service.windows[0][1] == datetime(
        2026, 9, 20, 7, 0, tzinfo=zone,
    ).astimezone(UTC)


async def test_reconciliation_single_flight_prevents_duplicate_provider_calls(settings):
    factory = init_db(settings)
    service = Service([row()])
    provider = SequenceProvider(delay=0.05)
    reconciler = MorningDigestReconciler(service, factory, settings, provider=provider)
    now = datetime(2026, 9, 20, 7, 0, tzinfo=ZoneInfo(settings.timezone))

    results = await asyncio.gather(*(reconciler.reconcile(now=now) for _ in range(3)))
    assert results == ["complete", "complete", "complete"]
    assert len(provider.calls) == 1


async def test_permanent_provider_error_is_suppressed_for_same_cutoff(settings):
    factory = init_db(settings)
    provider = SequenceProvider(failures=10, transient=False)
    reconciler = MorningDigestReconciler(
        Service([row()]), factory, settings, provider=provider,
    )
    now = datetime(2026, 9, 20, 7, 0, tzinfo=ZoneInfo(settings.timezone))

    assert await reconciler.reconcile(now=now) == "blocked"
    assert await reconciler.reconcile(now=now + timedelta(minutes=15)) == "blocked"
    assert len(provider.calls) == 1


async def test_next_successful_day_starts_at_previous_canonical_cutoff(settings):
    factory = init_db(settings)
    service = Service([row()])
    provider = Provider()
    first = datetime(2026, 9, 20, 7, 0, tzinfo=ZoneInfo(settings.timezone))
    second = first + timedelta(days=1)
    reconciler = MorningDigestReconciler(service, factory, settings, provider=provider)

    await reconciler.reconcile(now=first)
    await reconciler.reconcile(now=second)
    assert service.windows[1] == (first.astimezone(UTC), second.astimezone(UTC))


def test_digest_defaults():
    settings = Settings(_env_file=None)
    assert settings.digest_enabled is True
    assert settings.digest_time == "07:00"
    assert settings.digest_model == "gemini-3.8-flash"


def test_direct_gemini_provider_uses_key_schema_and_low_thinking():
    calls = []
    response = BytesIO(b'{"candidates":[{"content":{"parts":[{"text":'
                       b'"{\\"title\\":\\"Morning\\",\\"items\\":[]}"}]}}]}')

    def opener(request, timeout):
        calls.append((request, timeout))
        response.seek(0)
        return response

    settings = Settings(_env_file=None, gemini_api_key="secret-test-value")
    result = GeminiDigestProvider(settings, opener=opener).generate([{
        "ref": "m_a", "refs": ["m_a"], "timestamp": "2026-09-19T04:00:00Z",
        "source": "Course", "text": "Deadline at 18:00",
    }])
    assert result.title == "Morning"
    request, timeout = calls[0]
    assert timeout == 120
    assert request.full_url == (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        "gemini-3.8-flash:generateContent"
    )
    assert request.headers["X-goog-api-key"] == "secret-test-value"
    assert "Authorization" not in request.headers
    assert request.headers["Content-type"] == "application/json"
    body = json.loads(request.data)
    config = body["generationConfig"]
    assert config["thinkingConfig"] == {"thinkingLevel": "low"}
    assert not {"temperature", "topP", "topK"} & set(config)
    assert config["responseFormat"]["text"]["mimeType"] == "APPLICATION_JSON"
    assert config["responseFormat"]["text"]["schema"]
    assert '"mimeType": "application/json"' not in json.dumps(config)
    assert config["responseFormat"]["text"]["schema"]["properties"]["items"]["maxItems"] == 6


def test_direct_gemini_provider_fails_closed_without_key():
    settings = Settings(_env_file=None, gemini_api_key="")
    try:
        GeminiDigestProvider(settings).generate([{"ref": "m_a"}])
    except RuntimeError as exc:
        assert str(exc) == "Gemini API not configured"
    else:
        raise AssertionError("missing key must fail closed")


def test_direct_gemini_429_retries_are_bounded():
    attempts, sleeps = [], []

    def opener(request, timeout):
        attempts.append((request, timeout))
        raise HTTPError(request.full_url, 429, "rate limit", {}, None)

    settings = Settings(_env_file=None, gemini_api_key="secret-test-value")
    provider = GeminiDigestProvider(settings, opener=opener, sleeper=sleeps.append)
    try:
        provider.generate([{"ref": "m_a"}])
    except GeminiProviderError as exc:
        assert str(exc) == (
            "Gemini API request failed: HTTP 429 UNKNOWN: Request rejected"
        )
        assert exc.transient is True
    else:
        raise AssertionError("429 must fail after bounded retries")
    assert len(attempts) == 3 and sleeps == [1, 2]


def test_direct_gemini_503_remains_retryable():
    attempts = []

    def opener(request, timeout):
        attempts.append((request, timeout))
        raise HTTPError(request.full_url, 503, "unavailable", {}, None)

    settings = Settings(_env_file=None, gemini_api_key="secret-test-value")
    provider = GeminiDigestProvider(settings, opener=opener, sleeper=lambda _: None)
    try:
        provider.generate([{"ref": "m_a"}])
    except GeminiProviderError as exc:
        assert exc.transient is True
        assert "HTTP 503" in str(exc)
    else:
        raise AssertionError("503 must fail after bounded retries")
    assert len(attempts) == 3


def test_direct_gemini_error_is_sanitized_without_key_prompt_or_logs(caplog):
    secret = "-".join(("test", "credential"))

    def opener(request, timeout):
        del timeout
        body = json.loads(request.data)
        prompt = body["contents"][-1]["parts"][0]["text"]
        error = json.dumps({"error": {
            "code": 400,
            "status": "INVALID_ARGUMENT",
            "message": f"Rejected {secret} and {prompt}",
        }}).encode()
        raise HTTPError(request.full_url, 400, "bad request", {}, BytesIO(error))

    settings = Settings(_env_file=None, gemini_api_key=secret)
    provider = GeminiDigestProvider(settings, opener=opener)
    try:
        provider.generate([{"ref": "m_private", "text": "private prompt marker"}])
    except RuntimeError as exc:
        message = str(exc)
    else:
        raise AssertionError("HTTP 400 must fail")
    assert message.startswith("Gemini API request failed: HTTP 400 INVALID_ARGUMENT:")
    assert secret not in message
    assert "private prompt marker" not in message
    assert "[redacted]" in message
    assert not caplog.records
