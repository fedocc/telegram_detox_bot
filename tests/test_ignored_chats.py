from __future__ import annotations

import json
import logging
from datetime import date, datetime
from types import SimpleNamespace

import pytest

from app.cli import list_chats
from app.cli.check_ignored_chats import run as check_ignored_chats
from app.config import Settings
from app.db import repository
from app.db.session import init_db
from app.ignored_chats import IgnoredChatsConfigError, load_ignored_chats
from app.models.schemas import DailyDigest
from app.services.digest import generate_digest
from app.services.p0 import handle_p0_candidate
from app.telegram.client import ingest_event
from tests.fixtures.messages import msg


class NeverCalledLLM:
    def __init__(self) -> None:
        self.calls = 0

    def classify_p0(self, payload):
        self.calls += 1
        raise AssertionError("ignored chat reached P0 LLM")


class FakeEmail:
    def __init__(self) -> None:
        self.sent = []

    def send(self, subject, text, html=None, **kwargs) -> None:
        self.sent.append((subject, text, html))


ALLOWED_RETRY_CHAT_ID = "123456789"
IGNORED_RETRY_CHAT_ID = "-1001352932060"


@pytest.fixture()
def session_factory(settings):
    return init_db(settings)


@pytest.mark.parametrize("chat_id", ["12345", "-1009999999999"])
async def test_ignored_live_event_is_rejected_before_mapping_or_storage(
    settings,
    session_factory,
    chat_id,
    caplog,
) -> None:
    private_text = "ответь сейчас уникальный приватный текст"

    class IgnoredEvent:
        def __init__(self) -> None:
            self.chat_id = chat_id
            self.message = SimpleNamespace(raw_text=private_text)

        async def get_chat(self):
            raise AssertionError("ignored event chat was resolved")

        async def get_sender(self):
            raise AssertionError("ignored event sender was resolved")

    llm = NeverCalledLLM()
    email = FakeEmail()
    with caplog.at_level(logging.DEBUG):
        processed = await ingest_event(
            IgnoredEvent(),
            settings=settings,
            session_factory=session_factory,
            llm=llm,
            email=email,
            ignored_chat_ids={chat_id},
        )

    with session_factory() as session:
        assert repository.get_message(session, chat_id, 1) is None
        assert repository.pending_alert_jobs(session) == []
    assert processed is False
    assert llm.calls == 0
    assert email.sent == []
    assert private_text not in caplog.text


async def test_live_outgoing_message_is_context_only(
    settings,
    session_factory,
    monkeypatch,
    caplog,
) -> None:
    private_marker = "OUTGOING_CONTEXT_ONLY_MARKER"
    outgoing = msg(text=private_marker, is_outgoing=True)

    async def map_outgoing(event):
        return outgoing

    monkeypatch.setattr("app.telegram.client.event_to_stored_message", map_outgoing)
    llm = NeverCalledLLM()
    email = FakeEmail()

    with caplog.at_level(logging.DEBUG):
        processed = await ingest_event(
            SimpleNamespace(chat_id=outgoing.chat_id),
            settings=settings,
            session_factory=session_factory,
            llm=llm,
            email=email,
            ignored_chat_ids=set(),
        )

    with session_factory() as session:
        stored = repository.get_message(session, outgoing.chat_id, outgoing.message_id)
        assert stored is not None
        assert stored.is_outgoing is True
        assert stored.p0_classification is None
        assert repository.pending_alert_jobs(session) == []
    assert processed is True
    assert llm.calls == 0
    assert email.sent == []
    assert private_marker not in caplog.text


def test_ignored_chat_does_not_create_p0_or_context(session, settings, monkeypatch) -> None:
    message = msg(chat_id="ignored", text="ответь сейчас")
    repository.save_message(session, message)
    llm = NeverCalledLLM()
    email = FakeEmail()

    def fail_context(*args, **kwargs):
        raise AssertionError("ignored chat reached P0 context")

    monkeypatch.setattr(repository, "recent_chat_context", fail_context)

    assert handle_p0_candidate(
        session,
        message,
        llm,
        email,
        settings=settings,
        ignored_chat_ids={"ignored"},
    ) is False
    assert llm.calls == 0
    assert email.sent == []
    assert repository.pending_alert_jobs(session) == []


def test_pending_alert_from_newly_ignored_chat_is_not_retried(session, now) -> None:
    message = msg(chat_id="ignored", text="ответь сейчас", timestamp=now)
    repository.save_message(session, message)
    repository.mark_p0_classified(
        session,
        message.chat_id,
        message.message_id,
        "P0_STRICT",
        now,
        confidence=0.99,
    )
    repository.create_alert_job(
        session,
        chat_id=message.chat_id,
        message_id=message.message_id,
        alert_type="p0",
        subject="safe subject",
        text_body="safe body",
        html_body="",
        now=now,
    )
    email = FakeEmail()

    sent = repository.retry_pending_alerts(
        session,
        email,
        now,
        excluded_chat_ids={"ignored"},
    )

    assert sent == 0
    assert email.sent == []


def _pending_digest_with_chat_ids(session, chat_ids: list[str], private_marker: str):
    digest = DailyDigest(
        date="2026-07-07",
        direct_messages=[
            {
                "chat": f"Chat {index}",
                "summary": "Safe summary",
                "needs_reply": False,
                "source_refs": [{"chat_id": chat_id, "message_id": index}],
            }
            for index, chat_id in enumerate(chat_ids, start=1)
        ],
    )
    record = repository.save_digest(
        session,
        digest,
        f"<p>{private_marker}</p>",
        subject="[Telegram Detox][Digest] test",
        text=private_marker,
    )
    for index, chat_id in enumerate(chat_ids, start=1):
        message_id = record.id * 1000 + index
        repository.save_message(
            session,
            msg(
                chat_id=chat_id,
                message_id=message_id,
                text="synthetic incoming retry source",
            ),
        )
        stored = repository.get_message(session, chat_id, message_id)
        assert stored is not None
        stored.claimed_digest_id = record.id
    session.commit()
    return record


def test_old_pending_digest_without_source_metadata_is_cancelled(session, caplog) -> None:
    private_marker = "ignored old digest private marker"
    record = _pending_digest_with_chat_ids(
        session,
        [ALLOWED_RETRY_CHAT_ID],
        private_marker,
    )
    record.source_chat_ids = None
    session.commit()
    email = FakeEmail()

    with caplog.at_level(logging.INFO):
        sent = repository.retry_pending_digests(
            session,
            email,
            record.next_attempt_at,
            ignored_chat_ids={IGNORED_RETRY_CHAT_ID},
        )

    assert sent == 0
    assert email.sent == []
    assert record.email_status == "cancelled"
    assert record.subject == ""
    assert record.text_payload == ""
    assert record.html_payload == ""
    assert record.json_payload == ""
    assert private_marker not in caplog.text


@pytest.mark.parametrize(
    "chat_ids",
    [
        [IGNORED_RETRY_CHAT_ID],
        [IGNORED_RETRY_CHAT_ID, ALLOWED_RETRY_CHAT_ID],
    ],
)
def test_pending_digest_with_ignored_source_is_never_sent_verbatim(
    session,
    caplog,
    chat_ids,
) -> None:
    private_marker = "blacklisted digest content marker"
    record = _pending_digest_with_chat_ids(session, chat_ids, private_marker)
    email = FakeEmail()

    with caplog.at_level(logging.INFO):
        sent = repository.retry_pending_digests(
            session,
            email,
            record.next_attempt_at,
            ignored_chat_ids={IGNORED_RETRY_CHAT_ID},
        )

    assert sent == 0
    assert email.sent == []
    assert record.email_status == "cancelled"
    assert record.subject == ""
    assert record.text_payload == ""
    assert record.html_payload == ""
    assert record.json_payload == ""
    assert private_marker not in caplog.text


def test_pending_digest_with_only_allowed_sources_retries_normally(session) -> None:
    record = _pending_digest_with_chat_ids(
        session,
        [ALLOWED_RETRY_CHAT_ID],
        "allowed body",
    )
    email = FakeEmail()

    sent = repository.retry_pending_digests(
        session,
        email,
        record.next_attempt_at,
        ignored_chat_ids={IGNORED_RETRY_CHAT_ID},
    )

    assert sent == 1
    assert len(email.sent) == 1
    assert record.email_status == "sent"


def test_pending_digest_with_integer_source_id_retries_normally(session) -> None:
    record = _pending_digest_with_chat_ids(
        session,
        [ALLOWED_RETRY_CHAT_ID],
        "allowed body",
    )
    record.source_chat_ids = json.dumps([int(ALLOWED_RETRY_CHAT_ID)])
    session.commit()
    email = FakeEmail()

    sent = repository.retry_pending_digests(
        session,
        email,
        record.next_attempt_at,
        ignored_chat_ids={IGNORED_RETRY_CHAT_ID},
    )

    assert sent == 1
    assert len(email.sent) == 1
    assert record.email_status == "sent"


@pytest.mark.parametrize(
    "source_chat_ids",
    [
        [],
        [""],
        ["   "],
        [None],
        [{"chat_id": ALLOWED_RETRY_CHAT_ID}],
        ["abc"],
        [f" {ALLOWED_RETRY_CHAT_ID}"],
        [f"{ALLOWED_RETRY_CHAT_ID} "],
        ["0123"],
        [0],
        [True],
    ],
)
def test_invalid_digest_source_metadata_is_cancelled_and_scrubbed(
    session,
    caplog,
    source_chat_ids,
) -> None:
    private_marker = "unsafe retry private marker"
    record = _pending_digest_with_chat_ids(
        session,
        [ALLOWED_RETRY_CHAT_ID],
        private_marker,
    )
    record.source_chat_ids = json.dumps(source_chat_ids)
    session.commit()
    email = FakeEmail()

    with caplog.at_level(logging.INFO):
        sent = repository.retry_pending_digests(
            session,
            email,
            record.next_attempt_at,
            ignored_chat_ids={IGNORED_RETRY_CHAT_ID},
        )

    assert sent == 0
    assert email.sent == []
    assert record.email_status == "cancelled"
    assert record.subject == ""
    assert record.text_payload == ""
    assert record.html_payload == ""
    assert record.json_payload == ""
    assert private_marker not in caplog.text


@pytest.mark.parametrize(
    "stored_source_chat_ids",
    [
        json.dumps(None),
        json.dumps({"chat_id": ALLOWED_RETRY_CHAT_ID}),
        json.dumps(ALLOWED_RETRY_CHAT_ID),
        "not-json",
    ],
)
def test_non_list_digest_source_metadata_is_cancelled(
    session,
    stored_source_chat_ids,
) -> None:
    record = _pending_digest_with_chat_ids(
        session,
        [ALLOWED_RETRY_CHAT_ID],
        "unsafe body",
    )
    record.source_chat_ids = stored_source_chat_ids
    session.commit()
    email = FakeEmail()

    sent = repository.retry_pending_digests(
        session,
        email,
        record.next_attempt_at,
        ignored_chat_ids=set(),
    )

    assert sent == 0
    assert email.sent == []
    assert record.email_status == "cancelled"
    assert record.subject == ""
    assert record.text_payload == ""
    assert record.html_payload == ""
    assert record.json_payload == ""


def test_unsafe_digest_cancellation_releases_claimed_messages(session) -> None:
    message = msg(chat_id=ALLOWED_RETRY_CHAT_ID, message_id=811, text="safe fixture")
    repository.save_message(session, message)
    record = _pending_digest_with_chat_ids(
        session,
        [ALLOWED_RETRY_CHAT_ID],
        "unsafe body",
    )
    record.source_chat_ids = json.dumps([])
    stored_message = repository.get_message(
        session,
        message.chat_id,
        message.message_id,
    )
    assert stored_message is not None
    stored_message.claimed_digest_id = record.id
    session.commit()

    sent = repository.retry_pending_digests(
        session,
        FakeEmail(),
        record.next_attempt_at,
        ignored_chat_ids=set(),
    )

    session.refresh(stored_message)
    assert sent == 0
    assert stored_message.claimed_digest_id is None


def test_cleanup_cancels_unsafe_pending_digest_jobs(session) -> None:
    unknown = _pending_digest_with_chat_ids(
        session,
        [ALLOWED_RETRY_CHAT_ID],
        "unknown body",
    )
    unknown.source_chat_ids = None
    ignored = _pending_digest_with_chat_ids(
        session,
        [IGNORED_RETRY_CHAT_ID],
        "ignored body",
    )
    allowed = _pending_digest_with_chat_ids(
        session,
        [ALLOWED_RETRY_CHAT_ID],
        "allowed body",
    )
    session.commit()

    cancelled = repository.cancel_unsafe_pending_digests(
        session,
        {IGNORED_RETRY_CHAT_ID},
    )

    assert cancelled == 2
    assert unknown.email_status == "cancelled"
    assert ignored.email_status == "cancelled"
    assert allowed.email_status == "pending"


def test_ignored_legacy_row_is_excluded_from_digest_and_llm(session) -> None:
    class CapturingDigestLLM:
        def __init__(self) -> None:
            self.payload = None

        def daily_digest(self, payload: dict) -> DailyDigest:
            self.payload = payload
            return DailyDigest(date=payload["date"])

    repository.save_message(
        session,
        msg(chat_id="ignored", chat_title="Ignored", message_id=1, text="private ignored"),
    )
    repository.save_message(
        session,
        msg(chat_id="allowed", chat_title="Allowed", message_id=1, text="hello"),
    )
    llm = CapturingDigestLLM()

    digest = generate_digest(
        session,
        llm,
        date(2026, 7, 7),
        "Europe/Moscow",
        ignored_chat_ids={"ignored"},
    )

    assert {chat["chat_id"] for chat in llm.payload["chats"]} == {"allowed"}
    assert all(
        ref.chat_id != "ignored"
        for item in [*digest.direct_messages, *digest.group_updates, *digest.review]
        for ref in item.source_refs
    )


def test_ignore_chat_ids_environment_setting(monkeypatch) -> None:
    monkeypatch.setenv("IGNORE_CHAT_IDS", "-100111,-100222")

    settings = Settings(_env_file=None)

    assert settings.ignore_chat_ids == "-100111,-100222"


def test_ignored_chats_json_loads(tmp_path) -> None:
    path = tmp_path / "ignored_chats.json"
    path.write_text(
        json.dumps([{"chat_id": "-100123", "reason": "local"}]),
        encoding="utf-8",
    )

    config = load_ignored_chats(path=path)

    assert config.chat_ids == {"-100123"}
    assert config.invalid_id_count == 0


def test_environment_and_json_ignored_ids_merge_without_duplicates(tmp_path) -> None:
    path = tmp_path / "ignored_chats.json"
    path.write_text(
        json.dumps(
            [
                {"chat_id": "-100222", "reason": "duplicate"},
                {"chat_id": "-100333", "reason": "file"},
            ]
        ),
        encoding="utf-8",
    )

    config = load_ignored_chats("-100111,-100222", path)

    assert config.chat_ids == {"-100111", "-100222", "-100333"}


def test_invalid_ignored_chats_json_fails_safe_with_clear_error(tmp_path) -> None:
    path = tmp_path / "ignored_chats.json"
    path.write_text("not-json", encoding="utf-8")

    with pytest.raises(IgnoredChatsConfigError, match="Invalid ignored chats configuration"):
        load_ignored_chats(path=path)

    output = []
    result = check_ignored_chats(
        Settings(_env_file=None, ignored_chats_path=path),
        output=output.append,
    )
    assert result == 1
    assert output and output[0].startswith("ERROR: Invalid ignored chats configuration")


def test_check_ignored_chats_warns_without_printing_private_data(tmp_path) -> None:
    path = tmp_path / "ignored_chats.json"
    private_reason = "private reason marker"
    path.write_text(
        json.dumps(
            [
                {"chat_id": "-100123", "reason": private_reason},
                {"chat_id": "invalid", "reason": private_reason},
            ]
        ),
        encoding="utf-8",
    )
    output = []

    result = check_ignored_chats(
        Settings(_env_file=None, ignored_chats_path=path),
        output=output.append,
    )

    assert result == 0
    assert output == ["Ignored chats: 1", "WARNING: invalid chat IDs skipped: 1"]
    assert private_reason not in "\n".join(output)


async def test_list_chats_outputs_requested_metadata_and_ignored_flag(
    tmp_path,
    monkeypatch,
) -> None:
    path = tmp_path / "ignored_chats.json"
    path.write_text(
        json.dumps([{"chat_id": "-100123", "reason": "local"}]),
        encoding="utf-8",
    )
    entity = SimpleNamespace(username="project_chat", title="Project Chat")
    dialog = SimpleNamespace(
        id=-100123,
        entity=entity,
        name="Project Chat",
        date=datetime.fromisoformat("2026-07-07T12:00:00+03:00"),
        is_user=False,
        is_group=False,
        is_channel=True,
    )

    class FakeClient:
        async def connect(self) -> None:
            return None

        async def is_user_authorized(self) -> bool:
            return True

        async def iter_dialogs(self):
            yield dialog

        async def disconnect(self) -> None:
            return None

    monkeypatch.setattr(list_chats, "make_client", lambda settings: FakeClient())
    output = []

    listed = await list_chats.run(
        Settings(_env_file=None, ignored_chats_path=path),
        search="project",
        output=output.append,
    )
    record = json.loads(output[0])

    assert listed == 1
    assert record == {
        "chat_id": "-100123",
        "type": "channel",
        "title": "Project Chat",
        "username": "project_chat",
        "last_seen": "2026-07-07T12:00:00+03:00",
        "ignored": True,
    }
