from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import app.main as app_main
from app.db import repository
from app.db.session import init_db
from app.models.schemas import ChatType
from app.telegram import client as telegram_client
from tests.fixtures.messages import msg


class NeverCalledLLM:
    def classify_p0(self, payload):
        raise AssertionError("mention-only mode must not invoke the LLM")


class FakeEmail:
    def __init__(self) -> None:
        self.sent = []

    def send(self, subject, text, html=None, **kwargs) -> None:
        self.sent.append((subject, text, html))


@pytest.fixture()
def mention_settings(settings):
    return settings.model_copy(update={"mention_only_mode": True})


@pytest.fixture()
def session_factory(mention_settings):
    return init_db(mention_settings)


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["@fedocc", "@FEDOCC"])
async def test_exact_mentions_send_alert(
    mention_settings, session_factory, monkeypatch, text
) -> None:
    incoming = msg(text=text)

    async def map_event(event, **kwargs):
        return incoming

    monkeypatch.setattr(telegram_client, "event_to_stored_message", map_event)
    email = FakeEmail()

    processed = await telegram_client.ingest_event(
        SimpleNamespace(chat_id=incoming.chat_id),
        settings=mention_settings,
        session_factory=session_factory,
        llm=NeverCalledLLM(),
        email=email,
        ignored_chat_ids=set(),
    )

    assert processed is True
    assert [entry[0] for entry in email.sent] == ["Telegram alert"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text,reply_to_is_mine",
    [
        ("@fedocc_bot", False),
        ("@fedoccc", False),
        ("обычное личное сообщение", False),
        ("ответь", False),
        ("дедлайн сегодня", False),
        ("обычный ответ", True),
    ],
)
async def test_non_mentions_never_alert_or_persist_body(
    mention_settings,
    session_factory,
    monkeypatch,
    text,
    reply_to_is_mine,
) -> None:
    incoming = msg(text=text, reply_to_is_mine=reply_to_is_mine)

    async def map_event(event, **kwargs):
        return incoming

    monkeypatch.setattr(telegram_client, "event_to_stored_message", map_event)
    email = FakeEmail()

    processed = await telegram_client.ingest_event(
        SimpleNamespace(chat_id=incoming.chat_id),
        settings=mention_settings,
        session_factory=session_factory,
        llm=NeverCalledLLM(),
        email=email,
        ignored_chat_ids=set(),
    )

    with session_factory() as session:
        assert repository.get_message(session, incoming.chat_id, incoming.message_id) is None
    assert processed is False
    assert email.sent == []


@pytest.mark.asyncio
async def test_outgoing_mention_never_alerts(
    mention_settings, session_factory, monkeypatch
) -> None:
    outgoing = msg(text="@fedocc", is_outgoing=True)

    async def map_event(event, **kwargs):
        return outgoing

    monkeypatch.setattr(telegram_client, "event_to_stored_message", map_event)
    email = FakeEmail()

    assert not await telegram_client.ingest_event(
        SimpleNamespace(chat_id=outgoing.chat_id),
        settings=mention_settings,
        session_factory=session_factory,
        llm=NeverCalledLLM(),
        email=email,
        ignored_chat_ids=set(),
    )
    assert email.sent == []


@pytest.mark.asyncio
async def test_mention_in_ignored_chat_never_alerts(
    mention_settings, session_factory, monkeypatch
) -> None:

    async def map_event(event, **kwargs):
        raise AssertionError("ignored message must not be mapped")

    monkeypatch.setattr(telegram_client, "event_to_stored_message", map_event)
    email = FakeEmail()

    assert not await telegram_client.ingest_event(
        SimpleNamespace(chat_id="ignored"),
        settings=mention_settings,
        session_factory=session_factory,
        llm=NeverCalledLLM(),
        email=email,
        ignored_chat_ids={"ignored"},
    )
    assert email.sent == []


@pytest.mark.asyncio
async def test_mention_only_listener_never_constructs_llm(
    mention_settings, session_factory, monkeypatch
) -> None:
    class FakeClient:
        def on(self, event):
            return lambda handler: handler

        async def connect(self) -> None:
            pass

        async def is_user_authorized(self) -> bool:
            return True

        async def get_me(self):
            return SimpleNamespace(id=123)

        async def run_until_disconnected(self) -> None:
            pass

    async def fake_backfill(**kwargs) -> None:
        raise AssertionError("mention-only must not run backfill")

    def fail_llm(*args, **kwargs):
        raise AssertionError("mention-only listener must not construct an LLM client")

    monkeypatch.setattr(telegram_client, "make_client", lambda settings: FakeClient())
    monkeypatch.setattr(telegram_client, "run_startup_backfill", fake_backfill)
    monkeypatch.setattr("app.llm.client.HaikuClient", fail_llm)

    await telegram_client.run_listener(
        mention_settings,
        session_factory,
        ignored_chat_ids=set(),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("chat_type", [ChatType.private, ChatType.group, ChatType.channel])
async def test_mention_alerts_in_every_chat_type(
    mention_settings,
    session_factory,
    monkeypatch,
    chat_type,
) -> None:
    incoming = msg(text="ping @fedocc", chat_type=chat_type)

    async def map_event(event, **kwargs):
        return incoming

    monkeypatch.setattr(telegram_client, "event_to_stored_message", map_event)
    email = FakeEmail()

    assert await telegram_client.ingest_event(
        SimpleNamespace(chat_id=incoming.chat_id),
        settings=mention_settings,
        session_factory=session_factory,
        llm=NeverCalledLLM(),
        email=email,
        ignored_chat_ids=set(),
    )
    assert len(email.sent) == 1


@pytest.mark.parametrize("enabled", [True, False])
def test_mention_only_birthday_registration(
    mention_settings, monkeypatch, enabled
) -> None:
    jobs = []

    class FakeScheduler:
        def __init__(self, **kwargs) -> None:
            pass

        def add_job(self, func, *args, **kwargs) -> None:
            jobs.append((func.__name__, kwargs.get("id")))

        def start(self) -> None:
            pass

    async def fake_listener(*args, **kwargs) -> None:
        if enabled:
            kwargs["on_connected"](SimpleNamespace())
        else:
            assert kwargs["on_connected"] is None

    monkeypatch.setattr(app_main, "get_settings", lambda: mention_settings.model_copy(
        update={"birthday_reminders_enabled": enabled}
    ))
    monkeypatch.setattr(app_main, "configure_logging", lambda settings: None)
    monkeypatch.setattr(
        app_main,
        "load_ignored_chats_from_settings",
        lambda settings: SimpleNamespace(chat_ids=frozenset()),
    )
    monkeypatch.setattr(app_main, "init_db", lambda settings: lambda: None)
    monkeypatch.setattr(app_main, "AsyncIOScheduler", FakeScheduler)
    monkeypatch.setattr(app_main, "run_listener", fake_listener)

    asyncio.run(app_main.main())

    names = {name for name, _ in jobs}
    ids = {job_id for _, job_id in jobs}
    assert "daily_job" not in names
    assert "retry_digests_job" not in names
    assert ("birthday_daily_job" in names) is enabled
    assert ("birthday_poll" in ids) is enabled


@pytest.mark.asyncio
async def test_duplicate_mention_sends_once(mention_settings, session_factory, monkeypatch):
    incoming = msg(text="@fedocc @FEDOCC")

    async def map_event(event, **kwargs):
        assert kwargs == {"resolve_reply": False}
        return incoming

    monkeypatch.setattr(telegram_client, "event_to_stored_message", map_event)
    email = FakeEmail()
    for _ in range(2):
        await telegram_client.ingest_event(
            SimpleNamespace(chat_id=incoming.chat_id), settings=mention_settings,
            session_factory=session_factory, llm=NeverCalledLLM(), email=email,
            ignored_chat_ids=set(),
        )
    assert len(email.sent) == 1
    with session_factory() as session:
        record = repository.get_message(session, incoming.chat_id, incoming.message_id)
        assert record.p0_classified_at is None
        assert record.p0_llm_called_at is None


@pytest.mark.parametrize(
    "allowed,expected",
    [(None, ["p0"]), ({"mention_only"}, ["mention_only"]), (set(), []),
     ({"mention_only", "direct_reply"}, ["mention_only", "direct_reply"])],
)
def test_retry_type_filter(session, now, allowed, expected):
    from app.models.schemas import P0Status

    for index, alert_type in enumerate(["p0", "mention_only", "direct_reply"], 1):
        message = msg(message_id=index, text="@fedocc",
                      reply_to_message_id=42, reply_to_is_mine=True)
        repository.save_message(session, message)
        if alert_type == "p0":
            repository.mark_p0_classified(
                session, message.chat_id, index, P0Status.p0_strict.value, now,
                confidence=1.0,
            )
        repository.create_alert_job(
            session, chat_id=message.chat_id, message_id=index, alert_type=alert_type,
            subject="Telegram alert", text_body=alert_type, html_body="", now=now,
        )
    email = FakeEmail()
    assert repository.retry_pending_alerts(
        session, email, now, allowed_alert_types=allowed,
    ) == len(expected)
    assert [entry[1] for entry in email.sent] == expected
    assert repository.retry_pending_alerts(
        session, email, now, allowed_alert_types=allowed,
    ) == 0


@pytest.mark.parametrize("text,outgoing,ignored", [
    ("@fedocc_bot", False, False), ("@fedoccc", False, False),
    ("ответь", False, False), ("@fedocc", True, False), ("@fedocc", False, True),
])
def test_retry_rejects_unsafe_mentions(session, now, text, outgoing, ignored):
    message = msg(text=text, is_outgoing=outgoing)
    repository.save_message(session, message)
    repository.create_alert_job(
        session, chat_id=message.chat_id, message_id=message.message_id,
        alert_type="mention_only", subject="Telegram alert", text_body=text,
        html_body="", now=now,
    )
    email = FakeEmail()
    assert repository.retry_pending_alerts(
        session, email, now, allowed_alert_types={"mention_only"},
        excluded_chat_ids={message.chat_id} if ignored else set(),
    ) == 0
    assert email.sent == []


@pytest.mark.asyncio
async def test_backfill_mode_guard_never_reads_history(mention_settings):
    from app.telegram.backfill import run_startup_backfill

    stats = await run_startup_backfill(
        client=object(), settings=mention_settings, session_factory=None,
        llm=None, email_sender=None,
    )
    assert stats.messages_fetched == stats.chats_scanned == 0


def test_runtime_import_does_not_require_llm_sdk():
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-c", """
import sys
class RejectLLM:
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'openai' or fullname == 'app.llm.client':
            raise AssertionError('LLM runtime imported')
sys.meta_path.insert(0, RejectLLM())
import app.main
"""], capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("alert_type", ["mention_only", "direct_reply"])
def test_failed_deterministic_alert_waits_then_retries_once(session, now, alert_type):
    from datetime import timedelta

    class FailingEmail:
        def send(self, *args, **kwargs):
            raise RuntimeError("synthetic delivery failure")

    message = msg(text="@FEDOCC" if alert_type == "mention_only" else "reply",
                  reply_to_message_id=42, reply_to_is_mine=True)
    repository.save_message(session, message)
    job = repository.create_alert_job(
        session, chat_id=message.chat_id, message_id=message.message_id,
        alert_type=alert_type, subject="Telegram alert", text_body=message.text,
        html_body="", now=now,
    )
    assert not repository.send_alert_job(session, job, FailingEmail(), now)
    email = FakeEmail()
    assert not repository.send_alert_job(session, job, email, now)
    assert repository.retry_pending_alerts(
        session, email, now, allowed_alert_types={alert_type},
    ) == 0
    assert repository.retry_pending_alerts(
        session, email, now + timedelta(minutes=1), allowed_alert_types={alert_type},
    ) == 1
    assert repository.retry_pending_alerts(
        session, email, now + timedelta(minutes=2), allowed_alert_types={alert_type},
    ) == 0
    assert len(email.sent) == 1


def test_mention_retry_does_not_touch_legacy_claims(session, now):
    from datetime import timedelta

    job = repository.create_alert_job(
        session, chat_id="legacy", message_id=1, alert_type="p0",
        subject="old", text_body="old", html_body="", now=now,
    )
    repository.claim_pending_alert(session, job.id, now, "legacy-claim")
    assert repository.retry_pending_alerts(
        session, FakeEmail(), now + timedelta(minutes=20), allowed_alert_types={"mention_only"},
    ) == 0
    session.refresh(job)
    assert job.status == "sending"
    assert job.claim_token == "legacy-claim"  # noqa: S105 - synthetic job claim


@pytest.mark.asyncio
async def test_listener_filters_self_and_pre_start_messages(
    mention_settings, session_factory, monkeypatch,
):
    from datetime import UTC, datetime, timedelta

    seen = []

    class FakeClient:
        async def connect(self):
            pass

        async def is_user_authorized(self):
            return True

        async def get_me(self):
            return SimpleNamespace(id=123)

        def on(self, event):
            def register(handler):
                self.handler = handler
                return handler
            return register

        async def run_until_disconnected(self):
            for sender, outgoing, age in [(123, False, 0), (456, True, 0),
                                          (456, False, -60), (456, False, 1)]:
                await self.handler(SimpleNamespace(
                    sender_id=sender, out=outgoing,
                    date=datetime.now(UTC) + timedelta(seconds=age),
                ))

    async def ingest(event, **kwargs):
        seen.append(event)

    monkeypatch.setattr(telegram_client, "make_client", lambda settings: FakeClient())
    monkeypatch.setattr(telegram_client, "ingest_event", ingest)
    await telegram_client.run_listener(mention_settings, session_factory, ignored_chat_ids=set())
    assert len(seen) == 1
    assert seen[0].sender_id == 456


@pytest.mark.parametrize("mine,reply_id,outgoing,ignored", [
    (False, 42, False, False), (None, 42, False, False),
    (True, None, False, False), (True, 42, True, False), (True, 42, False, True),
])
def test_direct_reply_retry_rejects_unsafe_sources(session, now, mine, reply_id, outgoing, ignored):
    message = msg(text="reply", reply_to_is_mine=mine,
                  reply_to_message_id=reply_id, is_outgoing=outgoing)
    repository.save_message(session, message)
    repository.create_alert_job(
        session, chat_id=message.chat_id, message_id=message.message_id,
        alert_type="direct_reply", subject="Telegram alert", text_body="reply",
        html_body="", now=now,
    )
    email = FakeEmail()
    assert repository.retry_pending_alerts(
        session, email, now, allowed_alert_types={"mention_only", "direct_reply"},
        excluded_chat_ids={message.chat_id} if ignored else set(),
    ) == 0
    assert email.sent == []
