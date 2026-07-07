from __future__ import annotations

from datetime import date
from pathlib import Path

from app.db import repository
from app.email.render import render_html
from app.email.sender import EmailSendError
from app.llm.client import LLMError
from app.models.schemas import ChatType, DailyDigest, DigestNoiseCount, MediaType
from app.services.digest import (
    day_bounds,
    fallback_digest,
    generate_digest,
    send_daily_digest_pipeline,
)
from tests.fixtures.messages import msg


class FakeLLM:
    def daily_digest(self, payload: dict) -> DailyDigest:
        direct = []
        noise = []
        for chat in payload["chats"]:
            ids = [m["message_id"] for m in chat["messages"]]
            if chat["chat_type"] == "private":
                direct.append(
                    {
                        "chat": chat["chat_title"],
                        "summary": "Есть личное сообщение.",
                        "needs_reply": True,
                        "action": "Ответить.",
                        "deadline": None,
                        "priority": "P1",
                        "message_ids": ids,
                        "needs_manual_review": False,
                    }
                )
            else:
                noise.append({"chat": chat["chat_title"], "count": len(ids)})
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
                    "message_ids": [1],
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


def test_fallback_digest_keeps_direct_messages(now) -> None:
    digest = fallback_digest(date(2026, 7, 7), [msg(timestamp=now)])

    assert digest.direct_messages[0].needs_manual_review is True


def test_digest_cannot_drop_private_message_when_llm_omits_it(session) -> None:
    repository.save_message(session, msg(message_id=101, text="Ты сможешь сегодня?"))

    digest = generate_digest(session, OmittingLLM(), date(2026, 7, 7), "Europe/Moscow")

    assert digest.review
    item = digest.review[0]
    assert item.message_ids == [101]
    assert item.source_refs == [{"chat_id": "1", "message_id": 101}]
    assert item.reason == "LLM did not classify this incoming private message"
    assert item.sender == "Sender"
    assert item.raw_text == "Ты сможешь сегодня?"


def test_private_message_never_becomes_p3(session) -> None:
    repository.save_message(session, msg(message_id=102, text="личка"))

    digest = generate_digest(session, OmittingLLM(), date(2026, 7, 7), "Europe/Moscow")

    assert all(count.chat != "Маша" for count in digest.noise_counts)
    assert any(102 in item.message_ids for item in digest.review)


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
        and item.reason == "LLM did not classify this incoming private message"
        for item in digest.review
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
                        "message_ids": [1],
                        "source_refs": [{"chat_id": "p1", "message_id": 1}],
                    }
                ],
            )

    digest = generate_digest(session, OnePrivateOnlyLLM(), date(2026, 7, 7), "Europe/Moscow")

    assert any(item.source_refs == [{"chat_id": "p2", "message_id": 1}] for item in digest.review)


def test_digest_llm_failure_keeps_all_private_messages(session) -> None:
    repository.save_message(session, msg(message_id=201, text="one"))
    repository.save_message(session, msg(message_id=202, text="two"))

    digest = generate_digest(session, FailingLLM(), date(2026, 7, 7), "Europe/Moscow")

    ids = {mid for item in digest.review for mid in item.message_ids}
    assert {201, 202}.issubset(ids)
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

    sent = repository.retry_pending_digests(
        session,
        email,
        now=day_bounds(date(2026, 7, 7), "Europe/Moscow")[0],
    )

    assert sent == 1
    assert repository.pending_digests(session) == []
    assert email.sent


def test_digest_now_uses_persistent_delivery_pipeline() -> None:
    source = Path("app/cli/digest_now.py").read_text(encoding="utf-8")

    assert "send_daily_digest_pipeline" in source
    assert "send_and_store_digest" not in source


def test_fallback_digest_includes_private_messages(now) -> None:
    digest = fallback_digest(
        date(2026, 7, 7),
        [msg(message_id=401, text="secret personal", timestamp=now)],
    )

    assert digest.review[0].message_ids == [401]
    assert digest.review[0].raw_text == "secret personal"


def test_fallback_digest_includes_all_private_messages(now) -> None:
    digest = fallback_digest(
        date(2026, 7, 7),
        [
            msg(chat_id="p1", message_id=1, text="one", timestamp=now),
            msg(chat_id="p2", chat_title="Иван", message_id=1, text="two", timestamp=now),
        ],
    )

    refs = {tuple(item.source_refs[0].values()) for item in digest.review if item.source_refs}
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

    assert digest.review[0].reason == "P0 review candidate"
    assert digest.review[0].message_ids == [501]


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
    assert any(item.reason == "P0 review candidate" for item in digest.review)
