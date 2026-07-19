from __future__ import annotations

from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest
from telethon.tl.types import User

from app.db import repository
from app.db.session import init_db
from app.email.sender import EmailSendError
from app.llm.client import LLMError
from app.models.schemas import DailyDigest, P0Decision, P0Status
from app.services.digest import generate_digest
from app.telegram.backfill import run_startup_backfill
from tests.fixtures.messages import msg


class FakeDialog:
    def __init__(self, dialog_id: int, entity) -> None:
        self.id = dialog_id
        self.entity = entity


class FakeTelegramMessage:
    def __init__(
        self,
        *,
        message_id: int,
        text: str,
        timestamp: datetime,
        sender=None,
        out: bool = False,
        reply_parent=None,
    ) -> None:
        self.id = message_id
        self.raw_text = text
        self.date = timestamp
        self.out = out
        self.media = None
        self.reply_to_msg_id = getattr(reply_parent, "id", None)
        self._sender = sender
        self._reply_parent = reply_parent

    async def get_sender(self):
        return self._sender

    async def get_reply_message(self):
        return self._reply_parent


class FakeTelegramClient:
    def __init__(
        self,
        dialogs: list[FakeDialog],
        messages: dict[int, list[FakeTelegramMessage]],
    ) -> None:
        self.dialogs = dialogs
        self.messages = messages

    async def iter_dialogs(self):
        for dialog in self.dialogs:
            yield dialog

    async def iter_messages(
        self,
        entity,
        limit: int,
        reverse: bool = False,
        min_id: int | None = None,
        offset_date: datetime | None = None,
    ):
        count = 0
        rows = sorted(self.messages.get(entity.id, []), key=lambda item: item.id)
        if not reverse:
            rows = list(reversed(rows))
        for message in rows:
            if min_id is not None and message.id <= min_id:
                continue
            if offset_date is not None and message.date < offset_date:
                continue
            if count >= limit:
                break
            count += 1
            yield message


class FakeEmail:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.sent: list[tuple[str, str, str | None]] = []

    def send(self, subject: str, text: str, html: str | None = None, **kwargs) -> None:
        if self.fail:
            raise EmailSendError("down")
        self.sent.append((subject, text, html))


class FakeP0LLM:
    def __init__(self, status: P0Status = P0Status.not_p0, fail: bool = False) -> None:
        self.status = status
        self.fail = fail
        self.calls = 0

    def classify_p0(self, payload: dict) -> P0Decision:
        self.calls += 1
        if self.fail:
            raise LLMError("down")
        return P0Decision(
            status=self.status,
            summary="summary",
            action=None,
            confidence=0.8,
        )


class FakeDigestLLM:
    def daily_digest(self, payload: dict) -> DailyDigest:
        direct = []
        for chat in payload["chats"]:
            if chat["chat_type"] == "private":
                direct.append(
                    {
                        "chat": chat["chat_title"],
                        "summary": "Есть сообщения.",
                        "needs_reply": False,
                        "source_refs": [m["source_ref"] for m in chat["messages"]],
                    }
                )
        return DailyDigest(date=payload["date"], direct_messages=direct)


@pytest.fixture()
def session_factory(settings):
    return init_db(settings)


def _user(user_id: int, name: str) -> User:
    return User(id=user_id, first_name=name, last_name=None, username=None)


def _client_with_private_messages(messages: list[FakeTelegramMessage]) -> FakeTelegramClient:
    entity = _user(1, "Masha")
    return FakeTelegramClient([FakeDialog(1, entity)], {1: messages})


async def test_backfill_inserts_missed_messages(settings, session_factory, now) -> None:
    sender = _user(42, "Sender")
    client = _client_with_private_messages(
        [
            FakeTelegramMessage(message_id=2, text="new", timestamp=now, sender=sender),
            FakeTelegramMessage(
                message_id=1,
                text="old",
                timestamp=now - timedelta(minutes=5),
                sender=sender,
            ),
        ]
    )

    stats = await run_startup_backfill(
        client=client,
        settings=settings,
        session_factory=session_factory,
        llm=FakeP0LLM(),
        email_sender=FakeEmail(),
        now=now,
    )

    with session_factory() as session:
        rows = repository.messages_between(
            session,
            now - timedelta(days=1),
            now + timedelta(days=1),
        )
    assert stats.messages_inserted == 2
    assert {(row.chat_id, row.message_id) for row in rows} == {("1", 1), ("1", 2)}
    assert all(row.is_backfilled for row in rows)


async def test_backfill_skips_ignored_chat_before_state_or_message_processing(
    settings,
    session_factory,
    now,
) -> None:
    settings.ignore_chat_ids = "1"
    sender = _user(42, "Sender")
    client = _client_with_private_messages(
        [FakeTelegramMessage(message_id=1, text="ответь сейчас", timestamp=now, sender=sender)]
    )
    llm = FakeP0LLM(status=P0Status.p0_strict)
    email = FakeEmail()

    stats = await run_startup_backfill(
        client=client,
        settings=settings,
        session_factory=session_factory,
        llm=llm,
        email_sender=email,
        now=now,
    )

    with session_factory() as session:
        assert repository.get_message(session, "1", 1) is None
        assert repository.pending_backfill_states(session) == []
        assert repository.pending_alert_jobs(session) == []
    assert stats.messages_fetched == 0
    assert stats.messages_inserted == 0
    assert llm.calls == 0
    assert email.sent == []


async def test_backfill_stores_resolved_reply_to_outgoing_metadata(
    settings,
    session_factory,
    now,
) -> None:
    sender = _user(42, "Sender")
    outgoing_parent = SimpleNamespace(id=900, out=True)
    client = _client_with_private_messages(
        [
            FakeTelegramMessage(
                message_id=1,
                text="reply",
                timestamp=now,
                sender=sender,
                reply_parent=outgoing_parent,
            )
        ]
    )

    await run_startup_backfill(
        client=client,
        settings=settings,
        session_factory=session_factory,
        llm=FakeP0LLM(),
        email_sender=FakeEmail(),
        now=now,
    )

    with session_factory() as session:
        stored = repository.get_message(session, "1", 1)
    assert stored.reply_to_message_id == 900
    assert stored.reply_to_is_mine is True


async def test_backfill_running_twice_is_idempotent(settings, session_factory, now) -> None:
    sender = _user(42, "Sender")
    client = _client_with_private_messages(
        [FakeTelegramMessage(message_id=1, text="same", timestamp=now, sender=sender)]
    )

    first = await run_startup_backfill(
        client=client,
        settings=settings,
        session_factory=session_factory,
        llm=FakeP0LLM(),
        email_sender=FakeEmail(),
        now=now,
    )
    second = await run_startup_backfill(
        client=client,
        settings=settings,
        session_factory=session_factory,
        llm=FakeP0LLM(),
        email_sender=FakeEmail(),
        now=now,
    )

    assert first.messages_inserted == 1
    assert second.messages_inserted == 0


async def test_backfill_dedupes_by_chat_id_and_message_id(settings, session_factory, now) -> None:
    sender = _user(42, "Sender")
    with session_factory() as session:
        repository.save_message(session, msg(chat_id="2", message_id=1, text="other chat"))
    client = _client_with_private_messages(
        [
            FakeTelegramMessage(
                message_id=1,
                text="same id different chat",
                timestamp=now,
                sender=sender,
            )
        ]
    )

    stats = await run_startup_backfill(
        client=client,
        settings=settings,
        session_factory=session_factory,
        llm=FakeP0LLM(),
        email_sender=FakeEmail(),
        now=now,
    )

    with session_factory() as session:
        assert repository.get_message(session, "1", 1) is not None
        assert repository.get_message(session, "2", 1) is not None
    assert stats.messages_inserted == 1


async def test_backfill_respects_max_total_messages(settings, session_factory, now) -> None:
    settings.backfill_max_total_messages = 2
    sender = _user(42, "Sender")
    client = _client_with_private_messages(
        [
            FakeTelegramMessage(message_id=3, text="3", timestamp=now, sender=sender),
            FakeTelegramMessage(message_id=2, text="2", timestamp=now, sender=sender),
            FakeTelegramMessage(message_id=1, text="1", timestamp=now, sender=sender),
        ]
    )

    stats = await run_startup_backfill(
        client=client,
        settings=settings,
        session_factory=session_factory,
        llm=FakeP0LLM(),
        email_sender=FakeEmail(),
        now=now,
    )

    assert stats.messages_inserted == 2


async def test_backfill_pending_chat_keeps_frozen_window_after_total_limit(
    settings,
    session_factory,
    now,
) -> None:
    settings.backfill_max_total_messages = 1
    sender = _user(42, "Sender")
    first = _user(1, "First")
    second = _user(2, "Second")
    old_time = now - timedelta(hours=2)
    client = FakeTelegramClient(
        [FakeDialog(1, first), FakeDialog(2, second)],
        {
            1: [FakeTelegramMessage(message_id=1, text="first", timestamp=old_time, sender=sender)],
            2: [
                FakeTelegramMessage(
                    message_id=1,
                    text="second",
                    timestamp=old_time,
                    sender=sender,
                )
            ],
        },
    )

    first_stats = await run_startup_backfill(
        client=client,
        settings=settings,
        session_factory=session_factory,
        llm=FakeP0LLM(),
        email_sender=FakeEmail(),
        now=now,
    )
    settings.backfill_max_total_messages = 10
    second_stats = await run_startup_backfill(
        client=client,
        settings=settings,
        session_factory=session_factory,
        llm=FakeP0LLM(),
        email_sender=FakeEmail(),
        now=now + timedelta(hours=settings.backfill_hours + 1),
    )

    with session_factory() as session:
        assert repository.get_message(session, "1", 1) is not None
        assert repository.get_message(session, "2", 1) is not None
        rows = repository.messages_between(
            session,
            now - timedelta(days=2),
            now + timedelta(days=2),
            only_undigested=False,
        )
    assert first_stats.messages_inserted == 1
    assert second_stats.messages_inserted == 1
    assert len([(row.chat_id, row.message_id) for row in rows]) == 2
    assert len({(row.chat_id, row.message_id) for row in rows}) == 2


async def test_backfill_oldest_first_after_latest_does_not_skip_older_missed_messages(
    settings,
    session_factory,
    now,
) -> None:
    settings.backfill_max_messages_per_chat = 2
    sender = _user(42, "Sender")
    with session_factory() as session:
        repository.save_message(session, msg(chat_id="1", message_id=10, text="last stored"))
    client = _client_with_private_messages(
        [
            FakeTelegramMessage(message_id=13, text="newest", timestamp=now, sender=sender),
            FakeTelegramMessage(message_id=12, text="middle", timestamp=now, sender=sender),
            FakeTelegramMessage(message_id=11, text="oldest missed", timestamp=now, sender=sender),
        ]
    )

    await run_startup_backfill(
        client=client,
        settings=settings,
        session_factory=session_factory,
        llm=FakeP0LLM(),
        email_sender=FakeEmail(),
        now=now,
    )

    with session_factory() as session:
        assert repository.get_message(session, "1", 11) is not None
        assert repository.get_message(session, "1", 12) is not None
        assert repository.get_message(session, "1", 13) is None


async def test_backfill_per_chat_cap_is_per_run_not_lifetime_completion(
    settings,
    session_factory,
    now,
) -> None:
    settings.backfill_max_messages_per_chat = 2
    sender = _user(42, "Sender")
    client = _client_with_private_messages(
        [
            FakeTelegramMessage(message_id=1, text="one", timestamp=now, sender=sender),
            FakeTelegramMessage(message_id=2, text="two", timestamp=now, sender=sender),
            FakeTelegramMessage(message_id=3, text="three", timestamp=now, sender=sender),
        ]
    )

    first = await run_startup_backfill(
        client=client,
        settings=settings,
        session_factory=session_factory,
        llm=FakeP0LLM(),
        email_sender=FakeEmail(),
        now=now,
    )
    with session_factory() as session:
        states = repository.pending_backfill_states(session)
        rows_after_first = repository.messages_between(
            session,
            now - timedelta(days=1),
            now + timedelta(days=1),
            only_undigested=False,
        )
    second = await run_startup_backfill(
        client=client,
        settings=settings,
        session_factory=session_factory,
        llm=FakeP0LLM(),
        email_sender=FakeEmail(),
        now=now + timedelta(minutes=1),
    )
    with session_factory() as session:
        rows_after_second = repository.messages_between(
            session,
            now - timedelta(days=1),
            now + timedelta(days=1),
            only_undigested=False,
        )
        pending = repository.pending_backfill_states(session)

    assert first.messages_inserted == 2
    assert len(rows_after_first) == 2
    assert states and states[0].completed is False
    assert second.messages_inserted == 1
    assert {(row.chat_id, row.message_id) for row in rows_after_second} == {
        ("1", 1),
        ("1", 2),
        ("1", 3),
    }
    assert pending == []


async def test_old_backfilled_messages_do_not_trigger_immediate_p0_spam(
    settings,
    session_factory,
    now,
) -> None:
    sender = _user(42, "Sender")
    email = FakeEmail()
    llm = FakeP0LLM(status=P0Status.p0)
    client = _client_with_private_messages(
        [
            FakeTelegramMessage(
                message_id=1,
                text="Позвони через час",
                timestamp=now - timedelta(hours=2),
                sender=sender,
            )
        ]
    )

    await run_startup_backfill(
        client=client,
        settings=settings,
        session_factory=session_factory,
        llm=llm,
        email_sender=email,
        now=now,
    )

    with session_factory() as session:
        stored = repository.get_message(session, "1", 1)
    assert email.sent == []
    assert llm.calls == 0
    assert stored.p0_review_candidate is True
    assert stored.p0_classification == "P0_CANDIDATE"


async def test_old_outgoing_backfill_is_not_p0_candidate(
    settings,
    session_factory,
    now,
) -> None:
    sender = _user(42, "Sender")
    email = FakeEmail()
    llm = FakeP0LLM(status=P0Status.p0_strict)
    client = _client_with_private_messages(
        [
            FakeTelegramMessage(
                message_id=1,
                text="Позвони через час",
                timestamp=now - timedelta(hours=2),
                sender=sender,
                out=True,
            )
        ]
    )

    await run_startup_backfill(
        client=client,
        settings=settings,
        session_factory=session_factory,
        llm=llm,
        email_sender=email,
        now=now,
    )

    with session_factory() as session:
        stored = repository.get_message(session, "1", 1)
    assert email.sent == []
    assert llm.calls == 0
    assert stored.is_outgoing is True
    assert stored.p0_review_candidate is False
    assert stored.p0_classification is None


async def test_recent_outgoing_backfill_is_context_only_and_not_digested(
    settings,
    session_factory,
    now,
) -> None:
    sender = _user(42, "Sender")
    llm = FakeP0LLM(status=P0Status.p0_strict)
    email = FakeEmail()
    client = _client_with_private_messages(
        [
            FakeTelegramMessage(
                message_id=1,
                text="@fedocc срочно ответь",
                timestamp=now - timedelta(minutes=10),
                sender=sender,
                out=True,
            )
        ]
    )

    stats = await run_startup_backfill(
        client=client,
        settings=settings,
        session_factory=session_factory,
        llm=llm,
        email_sender=email,
        now=now,
    )

    class NeverDigestLLM:
        def daily_digest(self, payload):
            raise AssertionError("outgoing-only chat reached digest LLM")

    with session_factory() as session:
        stored = repository.get_message(session, "1", 1)
        digest = generate_digest(
            session,
            NeverDigestLLM(),
            date(2026, 7, 7),
            "Europe/Moscow",
        )

    assert stored is not None
    assert stored.is_outgoing is True
    assert stored.p0_classification is None
    assert stored.p0_review_candidate is False
    assert stats.p0_classifications_triggered == 0
    assert llm.calls == 0
    assert email.sent == []
    assert digest.direct_messages == []
    assert digest.group_updates == []


async def test_recent_backfilled_private_messages_can_trigger_p0_classification(
    settings,
    session_factory,
    now,
) -> None:
    sender = _user(42, "Sender")
    llm = FakeP0LLM(status=P0Status.not_p0)
    client = _client_with_private_messages(
        [
            FakeTelegramMessage(
                message_id=1,
                text="привет",
                timestamp=now - timedelta(minutes=10),
                sender=sender,
            )
        ]
    )

    stats = await run_startup_backfill(
        client=client,
        settings=settings,
        session_factory=session_factory,
        llm=llm,
        email_sender=FakeEmail(),
        now=now,
    )

    with session_factory() as session:
        stored = repository.get_message(session, "1", 1)
    assert llm.calls == 1
    assert stats.p0_classifications_triggered == 1
    assert stored.p0_classification == "NOT_P0"


async def test_backfilled_undigested_messages_appear_in_next_digest(
    settings,
    session_factory,
    now,
) -> None:
    sender = _user(42, "Sender")
    client = _client_with_private_messages(
        [FakeTelegramMessage(message_id=1, text="missed", timestamp=now, sender=sender)]
    )
    await run_startup_backfill(
        client=client,
        settings=settings,
        session_factory=session_factory,
        llm=FakeP0LLM(status=P0Status.not_p0),
        email_sender=FakeEmail(),
        now=now,
    )

    with session_factory() as session:
        digest = generate_digest(session, FakeDigestLLM(), date(2026, 7, 7), "Europe/Moscow")

    refs = {
        (ref.chat_id, ref.message_id)
        for item in digest.direct_messages
        for ref in item.source_refs
    }
    assert ("1", 1) in refs
