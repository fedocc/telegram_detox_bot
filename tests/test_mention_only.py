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

    async def map_event(event):
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

    async def map_event(event):
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

    async def map_event(event):
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

    async def map_event(event):
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

        async def run_until_disconnected(self) -> None:
            pass

    async def fake_backfill(**kwargs) -> None:
        assert kwargs["llm"] is None

    def fail_llm(*args, **kwargs):
        raise AssertionError("mention-only listener must not construct an LLM client")

    monkeypatch.setattr(telegram_client, "make_client", lambda settings: FakeClient())
    monkeypatch.setattr(telegram_client, "run_startup_backfill", fake_backfill)
    monkeypatch.setattr(telegram_client, "HaikuClient", fail_llm)

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

    async def map_event(event):
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


def test_mention_only_mode_does_not_register_digest_or_birthday_jobs(
    mention_settings, monkeypatch
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
        assert kwargs["on_connected"] is None

    monkeypatch.setattr(app_main, "get_settings", lambda: mention_settings.model_copy(
        update={"birthday_reminders_enabled": True}
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
    assert "birthday_daily_job" not in names
    assert "birthday_poll" not in ids
