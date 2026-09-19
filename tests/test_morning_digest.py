from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from io import BytesIO
from urllib.error import HTTPError

from app.config import Settings
from app.db.session import init_db
from app.inbox.digest import (
    DigestItem,
    DigestPayload,
    GeminiDigestProvider,
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
    assert timeout == 120 and settings.digest_model in request.full_url
    assert request.headers["X-goog-api-key"] == "secret-test-value"
    body = json.loads(request.data)
    config = body["generationConfig"]
    assert config["thinkingConfig"] == {"thinkingLevel": "low"}
    assert config["responseFormat"]["text"]["mimeType"] == "application/json"
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
    except RuntimeError as exc:
        assert str(exc) == "Gemini API request failed (HTTP 429)"
    else:
        raise AssertionError("429 must fail after bounded retries")
    assert len(attempts) == 3 and sleeps == [1, 2]
