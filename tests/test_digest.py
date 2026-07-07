from __future__ import annotations

from datetime import date

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
    assert item.reason == "LLM did not classify this incoming private message"
    assert item.sender == "Sender"
    assert item.raw_text == "Ты сможешь сегодня?"


def test_private_message_never_becomes_p3(session) -> None:
    repository.save_message(session, msg(message_id=102, text="личка"))

    digest = generate_digest(session, OmittingLLM(), date(2026, 7, 7), "Europe/Moscow")

    assert all(count.chat != "Маша" for count in digest.noise_counts)
    assert any(102 in item.message_ids for item in digest.review)


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


def test_fallback_digest_includes_private_messages(now) -> None:
    digest = fallback_digest(
        date(2026, 7, 7),
        [msg(message_id=401, text="secret personal", timestamp=now)],
    )

    assert digest.review[0].message_ids == [401]
    assert digest.review[0].raw_text == "secret personal"


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
