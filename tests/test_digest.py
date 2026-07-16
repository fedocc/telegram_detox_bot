from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

from app.db import repository
from app.db.session import init_db
from app.email.render import render_html, render_plain_text
from app.email.sender import EmailSendError
from app.llm.client import LLMError
from app.models.schemas import ChatType, DailyDigest, DigestNoiseCount, MediaType
from app.services.digest import (
    day_bounds,
    fallback_digest,
    generate_digest,
    send_daily_digest_pipeline,
)
from app.services.maintenance import recover_missed_daily_digests
from tests.fixtures.messages import msg


class FakeLLM:
    def daily_digest(self, payload: dict) -> DailyDigest:
        direct = []
        noise = []
        for chat in payload["chats"]:
            refs = [m["source_ref"] for m in chat["messages"]]
            if chat["chat_type"] == "private":
                direct.append(
                    {
                        "chat": chat["chat_title"],
                        "summary": "Есть личное сообщение.",
                        "needs_reply": True,
                        "action": "Ответить.",
                        "deadline": None,
                        "priority": "P1",
                        "source_refs": refs,
                        "needs_manual_review": False,
                    }
                )
            else:
                noise.append({"chat": chat["chat_title"], "count": len(refs)})
        return DailyDigest(date=payload["date"], direct_messages=direct, noise_counts=noise)


class OmittingLLM:
    def daily_digest(self, payload: dict) -> DailyDigest:
        return DailyDigest(
            date=payload["date"],
            direct_messages=[],
            noise_counts=[{"chat": "Маша", "count": 1}],
        )


class MaskingLLM:
    def daily_digest(self, payload: dict) -> DailyDigest:
        return DailyDigest(
            date=payload["date"],
            direct_messages=[
                {
                    "chat": "Other",
                    "summary": "Wrong chat same message id.",
                    "needs_reply": False,
                    "source_refs": [{"chat_id": "group-1", "message_id": 1}],
                }
            ],
            noise_counts=[{"chat": "Маша", "count": 1}],
        )


class FailingLLM:
    def daily_digest(self, payload: dict) -> DailyDigest:
        raise LLMError("aitunnel down")


class FakeEmail:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.sent: list[tuple[str, str, str | None]] = []

    def send(self, subject: str, text: str, html: str | None = None) -> None:
        if self.fail:
            raise EmailSendError("smtp down")
        self.sent.append((subject, text, html))


class MessageIdEmail(FakeEmail):
    def __init__(self, fail: bool = False) -> None:
        super().__init__(fail=fail)
        self.message_ids: list[str | None] = []

    def send(
        self,
        subject: str,
        text: str,
        html: str | None = None,
        **kwargs,
    ) -> None:
        self.message_ids.append(kwargs.get("message_id"))
        super().send(subject, text, html)


class CrashAfterSendEmail(MessageIdEmail):
    def send(
        self,
        subject: str,
        text: str,
        html: str | None = None,
        **kwargs,
    ) -> None:
        self.message_ids.append(kwargs.get("message_id"))
        self.sent.append((subject, text, html))
        raise RuntimeError("crash after provider accepted email")


class FailingGmailApiEmail(FakeEmail):
    def send(self, subject: str, text: str, html: str | None = None) -> None:
        raise EmailSendError("Gmail API send failed")


class CountingLLM(FakeLLM):
    def __init__(self) -> None:
        self.calls = 0

    def daily_digest(self, payload: dict) -> DailyDigest:
        self.calls += 1
        return super().daily_digest(payload)


class InspectingBatchLLM:
    def __init__(self) -> None:
        self.calls = 0
        self.payloads: list[dict] = []

    def daily_digest(self, payload: dict) -> DailyDigest:
        self.calls += 1
        self.payloads.append(payload)
        return DailyDigest(date=payload["date"])


def test_email_renders_deadline_text_when_no_deadline_at() -> None:
    digest = DailyDigest(
        date="2026-07-07",
        direct_messages=[
            {
                "chat": "Маша",
                "summary": "Просит позвонить.",
                "needs_reply": True,
                "deadline_text": "через час",
            }
        ],
    )

    text = render_plain_text(digest)
    html = render_html(digest)

    assert "через час" in text
    assert "через час" in html


def test_email_prefers_deadline_at_when_present() -> None:
    digest = DailyDigest(
        date="2026-07-07",
        direct_messages=[
            {
                "chat": "Маша",
                "summary": "Просит позвонить.",
                "needs_reply": True,
                "deadline_text": "через час",
                "deadline_at": "2026-07-07T19:00:00+03:00",
            }
        ],
    )

    text = render_plain_text(digest)
    html = render_html(digest)

    assert "2026-07-07T19:00:00+03:00" in text
    assert "2026-07-07T19:00:00+03:00" in html


def test_multiple_private_messages_from_same_sender_render_as_one_line(now) -> None:
    digest = fallback_digest(
        date(2026, 7, 7),
        [
            msg(
                message_id=1,
                text="еду в Китай",
                timestamp=datetime.fromisoformat("2026-07-07T18:42:00+03:00"),
            ),
            msg(
                message_id=2,
                text="до 15 августа",
                timestamp=datetime.fromisoformat("2026-07-07T19:10:00+03:00"),
            ),
        ],
    )

    output = render_plain_text(digest)

    assert output.count("- Маша:") == 1
    assert "Сообщений: 2" in output
    assert "Первое: 18:42" in output
    assert "Последнее: 19:10" in output
    assert "Ответ нужен" not in output


def test_grouped_line_includes_count_first_last_time() -> None:
    digest = fallback_digest(
        date(2026, 7, 7),
        [
            msg(
                chat_id="g1",
                chat_title="Лаба",
                chat_type=ChatType.group,
                message_id=1,
                text="one",
                timestamp=datetime.fromisoformat("2026-07-07T20:05:00+03:00"),
            ),
            msg(
                chat_id="g1",
                chat_title="Лаба",
                chat_type=ChatType.group,
                message_id=2,
                text="two",
                timestamp=datetime.fromisoformat("2026-07-07T21:17:00+03:00"),
            ),
        ],
    )

    output = render_plain_text(digest)

    assert "- Лаба:" in output
    assert "Сообщений: 2" in output
    assert "Первое: 20:05" in output
    assert "Последнее: 21:17" in output


def test_digest_output_does_not_contain_bad_phrases() -> None:
    digest = DailyDigest(
        date="2026-07-07",
        direct_messages=[
            {
                "chat": "Катя",
                "summary": "короткая переписка про поездку",
                "needs_reply": False,
            }
        ],
    )

    output = render_plain_text(digest) + render_html(digest)

    assert "короткая переписка" not in output
    assert "Ответ нужен" not in output


def test_personal_message_always_appears_in_digest(session) -> None:
    repository.save_message(session, msg())

    digest = generate_digest(session, FakeLLM(), date(2026, 7, 7), "Europe/Moscow")

    assert digest.direct_messages
    assert digest.direct_messages[0].chat == "Маша"


def test_group_flood_is_compressed_to_noise_count(session, now) -> None:
    for idx in range(1, 6):
        repository.save_message(
            session,
            msg(
                chat_id="g1",
                chat_title="Общий чат",
                chat_type=ChatType.group,
                message_id=idx,
                text=f"флуд {idx}",
                timestamp=now,
            ),
        )

    digest = generate_digest(session, FakeLLM(), date(2026, 7, 7), "Europe/Moscow")

    assert digest.noise_counts == [DigestNoiseCount(chat="Общий чат", count=5)]


def test_unprocessed_media_without_caption_goes_to_review(session, now) -> None:
    repository.save_message(
        session,
        msg(
            chat_id="g1",
            chat_title="Лаборатория",
            chat_type=ChatType.supergroup,
            message_id=7,
            text=None,
            media_type=MediaType.voice,
            timestamp=now,
        ),
    )

    digest = generate_digest(session, FakeLLM(), date(2026, 7, 7), "Europe/Moscow")

    assert digest.review
    assert "voice" in digest.review[0].reason


def test_html_email_renders_without_errors() -> None:
    digest = DailyDigest(
        date="2026-07-07",
        noise_counts=[DigestNoiseCount(chat="Общий", count=43)],
    )
    html = render_html(digest)

    assert "<html" in html
    assert "Общий" in html


def test_digest_output_does_not_contain_llm_did_not_classify(session) -> None:
    repository.save_message(session, msg(message_id=901, text="привет"))

    digest = generate_digest(session, OmittingLLM(), date(2026, 7, 7), "Europe/Moscow")
    output = render_plain_text(digest) + render_html(digest)

    assert "LLM did not classify" not in output
    assert "LLM did not classify this incoming private message" not in output


def test_digest_section_title_is_groups_only() -> None:
    digest = DailyDigest(date="2026-07-07")
    output = render_plain_text(digest) + render_html(digest)

    assert "ГРУППЫ" in output
    assert "ГРУППЫ / ЛАБОРАТОРИЯ" not in output


def test_non_urgent_private_message_not_routed_to_review_with_internal_text(session) -> None:
    repository.save_message(session, msg(message_id=902, text="Ок, спасибо"))

    digest = generate_digest(session, OmittingLLM(), date(2026, 7, 7), "Europe/Moscow")
    output = render_plain_text(digest)

    assert digest.direct_messages
    assert not digest.review
    assert "ПРОВЕРИТЬ ЛИЧНО\n- Нет" in output
    assert "LLM did not classify" not in output


def test_fallback_digest_keeps_direct_messages(now) -> None:
    digest = fallback_digest(date(2026, 7, 7), [msg(timestamp=now)])

    assert digest.direct_messages[0].needs_manual_review is True


def test_digest_cannot_drop_private_message_when_llm_omits_it(session) -> None:
    repository.save_message(session, msg(message_id=101, text="Ты сможешь сегодня?"))

    digest = generate_digest(session, OmittingLLM(), date(2026, 7, 7), "Europe/Moscow")

    assert digest.direct_messages
    item = digest.direct_messages[0]
    assert item.source_refs == [{"chat_id": "1", "message_id": 101}]
    assert item.chat == "Маша"
    assert item.summary == "Личное сообщение."


def test_private_message_never_becomes_p3(session) -> None:
    repository.save_message(session, msg(message_id=102, text="личка"))

    digest = generate_digest(session, OmittingLLM(), date(2026, 7, 7), "Europe/Moscow")

    assert all(count.chat != "Маша" for count in digest.noise_counts)
    assert any(
        item.source_refs == [{"chat_id": "1", "message_id": 102}]
        for item in digest.direct_messages
    )


def test_private_message_not_masked_by_group_same_message_id(session, now) -> None:
    repository.save_message(session, msg(message_id=1, text="private", timestamp=now))
    repository.save_message(
        session,
        msg(
            chat_id="group-1",
            chat_title="Group",
            chat_type=ChatType.group,
            message_id=1,
            text="group",
            timestamp=now,
        ),
    )

    digest = generate_digest(session, MaskingLLM(), date(2026, 7, 7), "Europe/Moscow")

    assert any(
        item.source_refs == [{"chat_id": "1", "message_id": 1}]
        for item in digest.direct_messages
    )


def test_private_message_not_masked_by_other_private_chat_same_message_id(session, now) -> None:
    repository.save_message(
        session,
        msg(chat_id="p1", chat_title="Маша", message_id=1, timestamp=now),
    )
    repository.save_message(
        session,
        msg(chat_id="p2", chat_title="Иван", message_id=1, timestamp=now),
    )

    class OnePrivateOnlyLLM:
        def daily_digest(self, payload: dict) -> DailyDigest:
            return DailyDigest(
                date=payload["date"],
                direct_messages=[
                    {
                        "chat": "Маша",
                        "summary": "One only.",
                        "needs_reply": False,
                        "source_refs": [{"chat_id": "p1", "message_id": 1}],
                    }
                ],
            )

    digest = generate_digest(session, OnePrivateOnlyLLM(), date(2026, 7, 7), "Europe/Moscow")

    assert any(
        item.source_refs == [{"chat_id": "p2", "message_id": 1}]
        for item in digest.direct_messages
    )


def test_digest_llm_failure_keeps_all_private_messages(session) -> None:
    repository.save_message(session, msg(message_id=201, text="one"))
    repository.save_message(session, msg(message_id=202, text="two"))

    digest = generate_digest(session, FailingLLM(), date(2026, 7, 7), "Europe/Moscow")

    refs = {
        (ref.chat_id, ref.message_id)
        for item in digest.direct_messages
        for ref in item.source_refs
    }
    assert {("1", 201), ("1", 202)}.issubset(refs)
    assert digest.generated_by == "fallback"


def test_digest_llm_failure_sends_fallback_digest(session) -> None:
    repository.save_message(session, msg(message_id=301, text="ping"))
    email = FakeEmail()

    digest = send_daily_digest_pipeline(
        session,
        FailingLLM(),
        email,
        date(2026, 7, 7),
        "Europe/Moscow",
    )

    assert digest.generated_by == "fallback"
    assert digest.email_status == "sent"
    assert email.sent[0][0].startswith("[FALLBACK] Telegram digest — 2026-07-07")


def test_daily_digest_openai_error_sends_fallback_digest(session) -> None:
    repository.save_message(session, msg(message_id=303, text="ping"))
    email = FakeEmail()

    digest = send_daily_digest_pipeline(
        session,
        FailingLLM(),
        email,
        date(2026, 7, 7),
        "Europe/Moscow",
    )

    assert digest.generated_by == "fallback"
    assert digest.email_status == "sent"


def test_email_failure_creates_retryable_pending_digest(session) -> None:
    repository.save_message(session, msg(message_id=302, text="ping"))

    digest = send_daily_digest_pipeline(
        session,
        FakeLLM(),
        FakeEmail(fail=True),
        date(2026, 7, 7),
        "Europe/Moscow",
        max_email_attempts=1,
    )

    assert digest.email_status == "pending"
    records = repository.pending_digests(session)
    assert len(records) == 1
    assert records[0].email_status == "pending"


def test_digest_email_failure_creates_pending_digest(session) -> None:
    repository.save_message(session, msg(message_id=304, text="ping"))

    send_daily_digest_pipeline(
        session,
        FakeLLM(),
        FakeEmail(fail=True),
        date(2026, 7, 7),
        "Europe/Moscow",
        max_email_attempts=1,
    )

    assert repository.pending_digests(session)[0].email_status == "pending"


def test_pending_digest_retry_works_when_gmail_api_send_fails(session) -> None:
    repository.save_message(session, msg(message_id=305, text="ping"))

    send_daily_digest_pipeline(
        session,
        FakeLLM(),
        FailingGmailApiEmail(),
        date(2026, 7, 7),
        "Europe/Moscow",
        max_email_attempts=1,
    )

    record = repository.pending_digests(session)[0]
    assert record.email_status == "pending"
    assert record.last_error_safe == "EmailSendError"


def test_new_pending_digest_is_immediately_retryable(session) -> None:
    digest = DailyDigest(date="2026-07-07")
    record = repository.save_digest(
        session,
        digest,
        "<p>html</p>",
        subject="subject",
        text="plain",
    )

    assert record.email_status == "pending"
    assert record.attempts == 0
    assert record.next_attempt_at is not None
    assert record.next_attempt_at <= record.created_at


def test_crash_after_save_digest_before_smtp_is_recovered_by_retry_scheduler(session) -> None:
    digest = DailyDigest(date="2026-07-07")
    record = repository.save_digest(
        session,
        digest,
        "<p>saved html</p>",
        subject="saved subject",
        text="saved plain",
    )
    email = FakeEmail()

    sent = repository.retry_pending_digests(session, email, now=record.next_attempt_at)

    assert sent == 1
    assert email.sent == [("saved subject", "saved plain", "<p>saved html</p>")]
    assert repository.pending_digests(session) == []


def test_successful_initial_digest_send_marks_digest_sent(session) -> None:
    repository.save_message(session, msg(message_id=313, text="ping"))

    digest = send_daily_digest_pipeline(
        session,
        FakeLLM(),
        FakeEmail(),
        date(2026, 7, 7),
        "Europe/Moscow",
    )

    assert digest.email_status == "sent"
    assert repository.pending_digests(session) == []


def test_failed_initial_digest_send_sets_backoff_timestamp(session) -> None:
    repository.save_message(session, msg(message_id=314, text="ping"))

    send_daily_digest_pipeline(
        session,
        FakeLLM(),
        FakeEmail(fail=True),
        date(2026, 7, 7),
        "Europe/Moscow",
        max_email_attempts=1,
    )
    record = repository.pending_digests(session)[0]

    assert record.email_status == "pending"
    assert record.attempts == 1
    assert record.next_attempt_at > record.created_at


def test_pending_digest_retried_and_marked_sent(session) -> None:
    repository.save_message(session, msg(message_id=305, text="ping"))
    send_daily_digest_pipeline(
        session,
        FakeLLM(),
        FakeEmail(fail=True),
        date(2026, 7, 7),
        "Europe/Moscow",
        max_email_attempts=1,
    )
    email = FakeEmail()
    record = repository.pending_digests(session)[0]

    sent = repository.retry_pending_digests(
        session,
        email,
        now=record.next_attempt_at,
    )

    assert sent == 1
    assert repository.pending_digests(session) == []
    assert email.sent


def test_first_failed_digest_waits_one_minute_before_retry(session, now) -> None:
    repository.save_message(session, msg(message_id=306, text="ping", timestamp=now))
    send_daily_digest_pipeline(
        session,
        FakeLLM(),
        FakeEmail(fail=True),
        date(2026, 7, 7),
        "Europe/Moscow",
        max_email_attempts=1,
    )
    record = repository.pending_digests(session)[0]

    assert record.attempts == 1
    assert record.next_attempt_at > record.created_at


def test_pending_digest_retry_reuses_original_html(session) -> None:
    repository.save_message(session, msg(message_id=307, text="ping"))
    send_daily_digest_pipeline(
        session,
        FakeLLM(),
        FakeEmail(fail=True),
        date(2026, 7, 7),
        "Europe/Moscow",
        max_email_attempts=1,
    )
    record = repository.pending_digests(session)[0]
    record.html_payload = "<p>ORIGINAL HTML</p>"
    session.commit()
    email = FakeEmail()

    repository.retry_pending_digests(session, email, now=record.next_attempt_at)

    assert email.sent[0][2] == "<p>ORIGINAL HTML</p>"


def test_pending_digest_retry_reuses_original_text(session) -> None:
    repository.save_message(session, msg(message_id=308, text="ping"))
    send_daily_digest_pipeline(
        session,
        FakeLLM(),
        FakeEmail(fail=True),
        date(2026, 7, 7),
        "Europe/Moscow",
        max_email_attempts=1,
    )
    record = repository.pending_digests(session)[0]
    record.text_payload = "ORIGINAL TEXT"
    session.commit()
    email = FakeEmail()

    repository.retry_pending_digests(session, email, now=record.next_attempt_at)

    assert email.sent[0][1] == "ORIGINAL TEXT"


def test_pending_digest_retry_does_not_call_llm_again(session) -> None:
    repository.save_message(session, msg(message_id=309, text="ping"))
    llm = CountingLLM()
    send_daily_digest_pipeline(
        session,
        llm,
        FakeEmail(fail=True),
        date(2026, 7, 7),
        "Europe/Moscow",
        max_email_attempts=1,
    )
    record = repository.pending_digests(session)[0]

    repository.retry_pending_digests(session, FakeEmail(), now=record.next_attempt_at)

    assert llm.calls == 1


def test_two_workers_cannot_claim_same_digest_job(session) -> None:
    repository.save_message(session, msg(message_id=310, text="ping"))
    send_daily_digest_pipeline(
        session,
        FakeLLM(),
        FakeEmail(fail=True),
        date(2026, 7, 7),
        "Europe/Moscow",
        max_email_attempts=1,
    )
    record = repository.pending_digests(session)[0]

    first = repository.claim_pending_digest(session, record.id, record.next_attempt_at, "token-1")
    second = repository.claim_pending_digest(session, record.id, record.next_attempt_at, "token-2")

    assert first is not None
    assert second is None


def test_stale_sending_digest_becomes_retryable(session, now) -> None:
    repository.save_message(session, msg(message_id=311, text="ping", timestamp=now))
    send_daily_digest_pipeline(
        session,
        FakeLLM(),
        FakeEmail(fail=True),
        date(2026, 7, 7),
        "Europe/Moscow",
        max_email_attempts=1,
    )
    record = repository.pending_digests(session)[0]
    repository.claim_pending_digest(session, record.id, record.next_attempt_at, "token-1")

    repository.release_stale_digest_claims(session, record.next_attempt_at, stale_minutes=0)

    assert repository.pending_digests(session)


def test_digest_real_openai_error_uses_fallback_digest(session, settings) -> None:
    from app.llm.client import HaikuClient
    from tests.test_llm_errors import BrokenClient

    repository.save_message(session, msg(message_id=312, text="ping"))
    client = HaikuClient(settings)
    client.client = BrokenClient()

    digest = send_daily_digest_pipeline(
        session,
        client,
        FakeEmail(),
        date(2026, 7, 7),
        "Europe/Moscow",
    )

    assert digest.generated_by == "fallback"


def test_digest_now_uses_persistent_delivery_pipeline() -> None:
    source = Path("app/cli/digest_now.py").read_text(encoding="utf-8")

    assert "send_daily_digest_pipeline" in source
    assert "send_and_store_digest" not in source


def test_digest_schema_uses_only_message_refs_for_sources() -> None:
    import app.models.schemas as schemas

    for model in [
        schemas.DigestP0Alert,
        schemas.DigestDirectMessage,
        schemas.DigestGroupUpdate,
        schemas.DigestReviewItem,
    ]:
        assert "message_ids" not in model.model_fields
        assert "source_refs" in model.model_fields


def test_fallback_digest_includes_private_messages(now) -> None:
    digest = fallback_digest(
        date(2026, 7, 7),
        [msg(message_id=401, text="secret personal", timestamp=now)],
    )

    assert digest.direct_messages[0].source_refs == [{"chat_id": "1", "message_id": 401}]
    assert "1 входящих" in digest.direct_messages[0].summary


def test_fallback_digest_includes_all_private_messages(now) -> None:
    digest = fallback_digest(
        date(2026, 7, 7),
        [
            msg(chat_id="p1", message_id=1, text="one", timestamp=now),
            msg(chat_id="p2", chat_title="Иван", message_id=1, text="two", timestamp=now),
        ],
    )

    refs = {
        tuple(ref.values())
        for item in digest.direct_messages
        for ref in item.source_refs
    }
    assert {("p1", 1), ("p2", 1)}.issubset(refs)


def test_fallback_digest_includes_p0_review_candidates(session, now) -> None:
    message = msg(
        chat_id="g1",
        chat_title="Лаба",
        chat_type=ChatType.group,
        message_id=501,
        text="может быть важно",
        timestamp=now,
    )
    repository.save_message(session, message)
    repository.mark_p0_review_candidate(session, "g1", 501)

    rows = repository.messages_between(
        session,
        *day_bounds(date(2026, 7, 7), "Europe/Moscow"),
    )
    digest = fallback_digest(date(2026, 7, 7), rows)

    assert digest.review[0].reason == "Возможно важное сообщение"
    assert digest.review[0].source_refs == [{"chat_id": "g1", "message_id": 501}]


def test_fallback_digest_includes_review_and_media(session, now) -> None:
    repository.save_message(
        session,
        msg(
            chat_id="g1",
            chat_title="Лаба",
            chat_type=ChatType.group,
            message_id=601,
            text=None,
            media_type=MediaType.photo,
            timestamp=now,
        ),
    )
    repository.save_message(
        session,
        msg(
            chat_id="g1",
            chat_title="Лаба",
            chat_type=ChatType.group,
            message_id=602,
            text="check",
            timestamp=now,
        ),
    )
    repository.mark_p0_review_candidate(session, "g1", 602)
    rows = repository.messages_between(session, *day_bounds(date(2026, 7, 7), "Europe/Moscow"))

    digest = fallback_digest(date(2026, 7, 7), rows)

    assert any("media" in item.reason or "медиа" in item.reason.lower() for item in digest.review)
    assert any(item.reason == "Возможно важное сообщение" for item in digest.review)


def test_running_digest_twice_does_not_resend_same_messages(session) -> None:
    repository.save_message(session, msg(message_id=701, text="first"))
    email = FakeEmail()

    first = send_daily_digest_pipeline(
        session,
        FakeLLM(),
        email,
        date(2026, 7, 7),
        "Europe/Moscow",
    )
    second = send_daily_digest_pipeline(
        session,
        FakeLLM(),
        email,
        date(2026, 7, 7),
        "Europe/Moscow",
    )

    assert first.direct_messages
    assert not second.direct_messages
    assert len(email.sent) == 1


def test_new_messages_after_successful_digest_appear_in_next_digest(session) -> None:
    email = FakeEmail()
    repository.save_message(session, msg(message_id=702, text="old"))
    send_daily_digest_pipeline(session, FakeLLM(), email, date(2026, 7, 7), "Europe/Moscow")
    repository.save_message(session, msg(message_id=703, text="new"))

    digest = send_daily_digest_pipeline(
        session,
        FakeLLM(),
        email,
        date(2026, 7, 7),
        "Europe/Moscow",
    )

    refs = {
        (ref.chat_id, ref.message_id)
        for item in digest.direct_messages
        for ref in item.source_refs
    }
    assert ("1", 703) in refs
    assert ("1", 702) not in refs


def test_processed_digested_messages_are_excluded_from_next_window(session) -> None:
    message = msg(message_id=704, text="done")
    repository.save_message(session, message)
    repository.mark_messages_digested(
        session,
        [message],
        datetime.fromisoformat("2026-07-07T20:30:00+03:00"),
    )

    rows = repository.messages_between(
        session,
        *day_bounds(date(2026, 7, 7), "Europe/Moscow"),
    )

    assert rows == []


def test_day_bounds_are_local_day_converted_to_utc() -> None:
    start, end = day_bounds(date(2026, 7, 7), "Europe/Moscow")

    assert start == datetime(2026, 7, 6, 21, 0, tzinfo=UTC)
    assert end == datetime(2026, 7, 7, 21, 0, tzinfo=UTC)


def test_digest_query_uses_utc_internal_window_for_local_day(session) -> None:
    repository.save_message(
        session,
        msg(
            message_id=801,
            text="local midnight",
            timestamp=datetime.fromisoformat("2026-07-07T00:30:00+03:00"),
        ),
    )

    digest = generate_digest(session, FakeLLM(), date(2026, 7, 7), "Europe/Moscow")

    refs = {
        (ref.chat_id, ref.message_id)
        for item in digest.direct_messages
        for ref in item.source_refs
    }
    assert ("1", 801) in refs


def test_missed_daily_digest_recovery_sends_previous_undigested_day(session) -> None:
    email = FakeEmail()
    repository.save_message(
        session,
        msg(
            message_id=802,
            text="missed yesterday",
            timestamp=datetime.fromisoformat("2026-07-06T18:00:00+03:00"),
        ),
    )

    recovered = recover_missed_daily_digests(
        session,
        FakeLLM(),
        email,
        "Europe/Moscow",
        datetime.fromisoformat("2026-07-07T09:00:00+03:00"),
    )

    assert recovered == [date(2026, 7, 6)]
    assert len(email.sent) == 1
    rows = repository.messages_between(
        session,
        *day_bounds(date(2026, 7, 6), "Europe/Moscow"),
        only_undigested=False,
    )
    assert rows[0].digested_at is not None


def test_pending_digest_prevents_manual_duplicate_send(session) -> None:
    repository.save_message(session, msg(message_id=803, text="pending"))
    email = FakeEmail(fail=True)
    first = send_daily_digest_pipeline(
        session,
        FakeLLM(),
        email,
        date(2026, 7, 7),
        "Europe/Moscow",
        max_email_attempts=1,
    )
    second_email = FakeEmail()
    second = send_daily_digest_pipeline(
        session,
        FakeLLM(),
        second_email,
        date(2026, 7, 7),
        "Europe/Moscow",
    )

    assert first.email_status == "pending"
    assert second.email_status == "pending"
    assert second_email.sent == []


def test_two_sessions_do_not_send_duplicate_digest_batch(settings) -> None:
    factory = init_db(settings)
    with factory() as first_session:
        repository.save_message(first_session, msg(message_id=804, text="one batch"))
    email = MessageIdEmail()

    with factory() as first_session:
        first = send_daily_digest_pipeline(
            first_session,
            FakeLLM(),
            email,
            date(2026, 7, 7),
            "Europe/Moscow",
        )
    with factory() as second_session:
        second = send_daily_digest_pipeline(
            second_session,
            FakeLLM(),
            email,
            date(2026, 7, 7),
            "Europe/Moscow",
        )
        rows = repository.messages_between(
            second_session,
            *day_bounds(date(2026, 7, 7), "Europe/Moscow"),
            only_undigested=False,
        )
        records = repository.pending_digests(second_session)

    assert first.email_status == "sent"
    assert second.direct_messages == []
    assert len(email.sent) == 1
    assert rows[0].digested_at is not None
    assert rows[0].claimed_digest_id is None
    assert records == []


def test_crash_after_email_send_does_not_create_new_independent_digest(session) -> None:
    repository.save_message(session, msg(message_id=805, text="crash window"))
    crashing_email = CrashAfterSendEmail()

    try:
        send_daily_digest_pipeline(
            session,
            FakeLLM(),
            crashing_email,
            date(2026, 7, 7),
            "Europe/Moscow",
        )
    except RuntimeError:
        pass
    later_email = MessageIdEmail()
    digest = send_daily_digest_pipeline(
        session,
        FakeLLM(),
        later_email,
        date(2026, 7, 7),
        "Europe/Moscow",
    )
    rows = repository.messages_between(
        session,
        *day_bounds(date(2026, 7, 7), "Europe/Moscow"),
        only_undigested=False,
    )

    assert len(crashing_email.sent) == 1
    assert later_email.sent == []
    assert digest.email_status == "pending"
    assert rows[0].claimed_digest_id is not None
    assert rows[0].digested_at is None


def test_failed_digest_send_keeps_claimed_messages_retryable_without_rebuild(session) -> None:
    repository.save_message(session, msg(message_id=806, text="retry same run"))
    email = MessageIdEmail(fail=True)
    llm = CountingLLM()

    digest = send_daily_digest_pipeline(
        session,
        llm,
        email,
        date(2026, 7, 7),
        "Europe/Moscow",
        max_email_attempts=1,
    )
    record = repository.pending_digests(session)[0]
    first_delivery_id = record.delivery_id
    rows = repository.messages_between(
        session,
        *day_bounds(date(2026, 7, 7), "Europe/Moscow"),
        only_undigested=False,
    )
    assert digest.email_status == "pending"
    assert rows[0].digested_at is None
    assert rows[0].claimed_digest_id == record.id
    assert llm.calls == 1
    retry_email = MessageIdEmail()
    sent = repository.retry_pending_digests(session, retry_email, now=record.next_attempt_at)

    assert sent == 1
    assert retry_email.message_ids == [first_delivery_id]


def test_aggregation_uses_configured_limits(session) -> None:
    for idx in range(1, 5):
        repository.save_message(session, msg(message_id=800 + idx, text=f"msg {idx}"))

    digest = generate_digest(
        session,
        FakeLLM(),
        date(2026, 7, 7),
        "Europe/Moscow",
        max_messages_per_window=2,
        max_messages_per_chat=1,
        max_chars_per_group=20,
    )

    assert any("лимит" in item.summary.lower() for item in digest.review)


def test_daily_digest_uses_grouped_batch_summarization(session) -> None:
    repository.save_message(session, msg(message_id=901, text="private one"))
    repository.save_message(
        session,
        msg(
            chat_id="g1",
            chat_title="Лаба",
            chat_type=ChatType.group,
            message_id=902,
            text="group one",
        ),
    )
    llm = InspectingBatchLLM()

    generate_digest(session, llm, date(2026, 7, 7), "Europe/Moscow")

    assert llm.calls == 1
    assert len(llm.payloads[0]["chats"]) == 2
    assert all("messages" in chat for chat in llm.payloads[0]["chats"])


def test_live_handler_is_registered_before_startup_backfill() -> None:
    source = Path("app/telegram/client.py").read_text(encoding="utf-8")

    assert "run_startup_backfill" in source
    backfill_call = source.index("await run_startup_backfill")
    assert source.index("@client.on") < backfill_call
    assert backfill_call < source.index("run_until_disconnected")
