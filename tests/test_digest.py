from __future__ import annotations

import logging
import re
from datetime import UTC, date, datetime
from pathlib import Path

from app.db import repository
from app.db.session import init_db
from app.email.render import digest_subject, render_html, render_plain_text
from app.email.sender import EmailSendError
from app.llm.client import LLMError
from app.models.schemas import ChatType, DailyDigest, DigestNoiseCount, MediaType, P0Status
from app.services.digest import (
    day_bounds,
    fallback_digest,
    generate_digest,
    send_daily_digest_pipeline,
)
from app.services.text import sanitize_channel_summary
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


class SemanticLLM:
    def daily_digest(self, payload: dict) -> DailyDigest:
        refs = payload["chats"][0]["messages"]
        return DailyDigest(
            date=payload["date"],
            direct_messages=[
                {
                    "chat": "Маша",
                    "summary": "Обсуждение поездки в Китай.",
                    "what_happened": "Обсуждение поездки в Китай.",
                    "requests_to_me": "Подтвердить даты.",
                    "important_context": "Уезжает послезавтра.",
                    "action_items": "Ответить сегодня.",
                    "should_open_telegram": True,
                    "open_reason": "Нужны детали маршрута.",
                    "needs_reply": False,
                    "source_refs": [item["source_ref"] for item in refs],
                }
            ],
        )


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

    assert "2026-07-07 19:00 MSK" in text
    assert "2026-07-07 19:00 MSK" in html


def test_digest_localizes_embedded_utc_iso_in_all_rendered_text() -> None:
    digest = DailyDigest(
        date="2026-07-20",
        direct_messages=[
            {
                "chat": "Synthetic private chat",
                "summary": "deadline: 2026-07-19T22:30:00Z",
                "needs_reply": True,
                "important_context": "context 2026-07-20T09:26:04+00:00",
                "action": "act after 2026-07-20T09:26:04.500+00:00",
                "deadline_text": "до 2026-07-20T09:26:04+00:00",
            }
        ],
    )

    rendered = render_plain_text(digest) + render_html(digest)

    assert "deadline: 2026-07-20 01:30 MSK" in rendered
    assert "context 2026-07-20 12:26 MSK" in rendered
    assert "act after 2026-07-20 12:26 MSK" in rendered
    assert "до 2026-07-20 12:26 MSK" in rendered
    assert "+00:00" not in rendered
    assert "2026-07-19T22:30:00Z" not in rendered
    assert not re.search(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:\+00:00|Z)", rendered)


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

    assert output.count("\nМаша\n") == 1
    assert "Сообщений: 2" in output
    assert "Время: 18:42–19:10 MSK" in output
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

    assert "\nЛаба\n" in output
    assert "Сообщений: 2" in output
    assert "Время: 20:05–21:17 MSK" in output


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
    repository.save_message(session, msg(text="Обычное обновление"))

    digest = generate_digest(session, FakeLLM(), date(2026, 7, 7), "Europe/Moscow")

    assert digest.direct_messages
    assert digest.direct_messages[0].chat == "Маша"


def test_outgoing_only_private_chat_is_excluded_from_digest(session) -> None:
    repository.save_message(
        session,
        msg(text="@fedocc срочно ответь", is_outgoing=True),
    )

    class NeverDigestLLM:
        def daily_digest(self, payload):
            raise AssertionError("outgoing-only chat reached digest LLM")

    digest = generate_digest(
        session,
        NeverDigestLLM(),
        date(2026, 7, 7),
        "Europe/Moscow",
    )

    assert digest.direct_messages == []
    assert digest.group_updates == []
    assert digest.review == []
    assert digest.diagnostics.messages_count == 0


def test_mixed_private_chat_digest_is_driven_only_by_incoming_messages(session) -> None:
    outgoing_text = "Федя просит срочно отправить файл"
    repository.save_message(
        session,
        msg(message_id=40, text=outgoing_text, is_outgoing=True),
    )
    repository.save_message(
        session,
        msg(message_id=41, text="Привет", is_outgoing=False),
    )

    class CapturingIncomingLLM(FakeLLM):
        def __init__(self) -> None:
            self.payload = None

        def daily_digest(self, payload: dict) -> DailyDigest:
            self.payload = payload
            return super().daily_digest(payload)

    llm = CapturingIncomingLLM()
    digest = generate_digest(session, llm, date(2026, 7, 7), "Europe/Moscow")
    output = render_plain_text(digest)

    assert llm.payload is not None
    payload_messages = llm.payload["chats"][0]["messages"]
    assert [item["message_id"] for item in payload_messages] == [41]
    assert payload_messages[0]["is_outgoing"] is False
    refs = digest.direct_messages[0].source_refs
    assert [(ref.chat_id, ref.message_id) for ref in refs] == [("1", 41)]
    assert outgoing_text not in output


def test_outgoing_only_chat_does_not_create_or_send_digest(session) -> None:
    repository.save_message(
        session,
        msg(text="завтра в 10 вылет ты готов?", is_outgoing=True),
    )
    email = FakeEmail()

    class NeverDigestLLM:
        def daily_digest(self, payload):
            raise AssertionError("outgoing-only chat reached digest pipeline")

    digest = send_daily_digest_pipeline(
        session,
        NeverDigestLLM(),
        email,
        date(2026, 7, 7),
        "Europe/Moscow",
    )

    assert digest.email_status == "sent"
    assert email.sent == []
    assert repository.pending_digest_for_date(session, "2026-07-07") is None
    stored = repository.get_message(session, "1", 1)
    assert stored is not None
    assert stored.claimed_digest_id is None
    assert stored.digested_at is None


def test_unknown_legacy_direction_does_not_drive_digest(session) -> None:
    repository.save_message(session, msg(text="legacy direction unknown"))
    stored = repository.get_message(session, "1", 1)
    assert stored is not None
    stored.is_outgoing = None
    session.commit()

    class NeverDigestLLM:
        def daily_digest(self, payload):
            raise AssertionError("unknown-direction row reached digest LLM")

    digest = generate_digest(
        session,
        NeverDigestLLM(),
        date(2026, 7, 7),
        "Europe/Moscow",
    )

    assert digest.direct_messages == []
    assert digest.group_updates == []


def test_existing_digest_claim_with_outgoing_driver_is_cancelled(session) -> None:
    repository.save_message(session, msg(text="legacy claimed self message"))
    row = repository.get_message(session, "1", 1)
    record, _, created = repository.claim_digest_run_for_rows(
        session,
        digest_date="2026-07-07",
        rows=[row],
    )
    assert record is not None and created is True
    row = repository.get_message(session, "1", 1)
    row.is_outgoing = True
    session.commit()
    email = FakeEmail()

    class NeverDigestLLM:
        def daily_digest(self, payload):
            raise AssertionError("unsafe legacy digest reached LLM")

    digest = send_daily_digest_pipeline(
        session,
        NeverDigestLLM(),
        email,
        date(2026, 7, 7),
        "Europe/Moscow",
    )

    session.refresh(record)
    session.refresh(row)
    assert digest.email_status == "sent"
    assert record.email_status == "cancelled"
    assert row.claimed_digest_id is None
    assert email.sent == []


def test_group_flood_gets_concise_summary_instead_of_noise_count(session, now) -> None:
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

    assert digest.noise_counts == []
    assert len(digest.group_updates) == 1
    assert digest.group_updates[0].chat == "Общий чат"
    assert "флуд" in digest.group_updates[0].summary
    assert digest.group_updates[0].message_count == 5


def test_unprocessed_media_without_caption_is_inside_group_summary(session, now) -> None:
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

    assert digest.review == []
    assert "голосовое" in digest.group_updates[0].media_summary


def test_media_noise_is_collapsed_per_chat(session, now) -> None:
    for message_id, media_type in enumerate(
        [MediaType.photo, MediaType.photo, MediaType.photo, MediaType.video, MediaType.voice],
        start=20,
    ):
        repository.save_message(
            session,
            msg(message_id=message_id, text=None, media_type=media_type, timestamp=now),
        )

    digest = generate_digest(session, FakeLLM(), date(2026, 7, 7), "Europe/Moscow")

    assert len(digest.direct_messages) == 1
    assert "3 фото" in digest.direct_messages[0].media_summary
    assert "1 видео" in digest.direct_messages[0].media_summary
    assert "1 голосовое" in digest.direct_messages[0].media_summary


def test_outgoing_media_is_not_added_to_media_summary(session, now) -> None:
    repository.save_message(
        session,
        msg(message_id=26, text=None, media_type=MediaType.video, is_outgoing=True, timestamp=now),
    )

    digest = generate_digest(session, FakeLLM(), date(2026, 7, 7), "Europe/Moscow")

    assert all(item.media_summary is None for item in digest.direct_messages)
    assert digest.review == []


def test_daily_digest_groups_private_messages_into_semantic_chat_summary(session) -> None:
    repository.save_message(session, msg(message_id=30, text="Поездка в Китай"))
    repository.save_message(session, msg(message_id=31, text="Я уезжаю послезавтра"))

    digest = generate_digest(session, SemanticLLM(), date(2026, 7, 7), "Europe/Moscow")
    output = render_plain_text(digest)

    assert len(digest.direct_messages) == 1
    assert len(digest.direct_messages[0].source_refs) == 2
    assert "Обсуждение поездки в Китай" in output
    assert "Важно: Подтвердить даты" in output
    assert "Уезжает послезавтра" in output
    assert "Открыть Telegram:" not in output


def test_html_email_renders_without_errors() -> None:
    digest = DailyDigest(
        date="2026-07-07",
        noise_counts=[DigestNoiseCount(chat="Общий", count=43)],
    )
    html = render_html(digest)

    assert "<html" in html
    assert "ФОН" not in html
    assert "Общий" not in html
    assert "КАНАЛЫ" in html


def test_email_omits_should_open_telegram_false() -> None:
    digest = DailyDigest(
        date="2026-07-07",
        direct_messages=[
            {
                "chat": "Маша",
                "summary": "Обсуждение планов.",
                "needs_reply": False,
                "what_happened": "Обсуждение планов.",
                "requests_to_me": "Нет.",
                "important_context": "Нет.",
                "action_items": "Нет.",
                "should_open_telegram": False,
                "open_reason": None,
            }
        ],
    )

    output = render_plain_text(digest) + render_html(digest)

    assert "Открыть Telegram:" not in output


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


def test_minimal_digest_has_exact_subject_and_four_sections() -> None:
    digest = DailyDigest(date="2026-07-07")
    output = render_plain_text(digest) + render_html(digest)

    assert digest_subject(digest) == "Telegram digest"
    assert "[Telegram Detox]" not in digest_subject(digest)
    for section in ("СРОЧНОЕ", "ЛИЧНЫЕ СООБЩЕНИЯ", "ГРУППЫ", "КАНАЛЫ"):
        assert section in output
    assert "ФОН" not in output
    assert "ПРОВЕРИТЬ ЛИЧНО" not in output


def test_empty_optional_item_fields_are_not_rendered() -> None:
    digest = DailyDigest(
        date="2026-07-07",
        direct_messages=[
            {
                "chat": "Synthetic private chat",
                "summary": "Короткое обновление.",
                "needs_reply": False,
                "requests_to_me": "Явных запросов нет.",
                "important_context": "Дополнительный контекст не выделен.",
                "action_items": "Действий по переписке не указано.",
                "should_open_telegram": False,
            }
        ],
    )

    output = render_plain_text(digest) + render_html(digest)

    assert "Открыть Telegram: нет" not in output
    assert "Явных запросов нет" not in output
    assert "Действий по переписке не указано" not in output
    assert "Дополнительный контекст не выделен" not in output
    assert "\n\nГРУППЫ" in render_plain_text(digest)


def test_optional_placeholder_filtering_ignores_case_spacing_and_punctuation() -> None:
    placeholders = (
        "Явных запросов нет",
        "Явных запросов нет.",
        "  ЯВНЫХ   ЗАПРОСОВ   НЕТ !  ",
        "Действий по переписке не указано",
        "Действий по переписке не указано.",
        "Дополнительный контекст не выделен",
        "Дополнительный контекст не выделен.",
        "Открыть Telegram: нет",
        "Открыть Telegram: нет.",
    )
    for placeholder in placeholders:
        digest = DailyDigest(
            date="2026-07-07",
            direct_messages=[
                {
                    "chat": "Synthetic private chat",
                    "summary": "Concrete fact is preserved.",
                    "action": placeholder,
                    "needs_reply": False,
                }
            ],
        )

        output = render_plain_text(digest) + render_html(digest)

        assert placeholder.strip() not in output
        assert "Concrete fact is preserved." in output


def test_non_urgent_private_message_not_routed_to_review_with_internal_text(session) -> None:
    repository.save_message(session, msg(message_id=902, text="Ок, спасибо"))

    digest = generate_digest(session, OmittingLLM(), date(2026, 7, 7), "Europe/Moscow")
    output = render_plain_text(digest)

    assert digest.direct_messages
    assert not digest.review
    assert "ПРОВЕРИТЬ ЛИЧНО" not in output
    assert "LLM did not classify" not in output


def test_fallback_digest_keeps_direct_messages(now) -> None:
    digest = fallback_digest(date(2026, 7, 7), [msg(timestamp=now)])

    assert digest.direct_messages[0].needs_manual_review is False
    assert digest.direct_messages[0].what_happened
    assert digest.direct_messages[0].action_items == "Открыть Telegram и ответить."
    assert "Действие не определено" not in digest.direct_messages[0].action_items


def test_fallback_question_does_not_claim_there_is_no_request(now) -> None:
    digest = fallback_digest(date(2026, 7, 7), [msg(text="сможешь помочь?", timestamp=now)])
    item = digest.direct_messages[0]

    assert item.requests_to_me == "ответить на вопрос"
    assert "Явного запроса не обнаружено" not in item.requests_to_me
    assert item.needs_reply is True


def test_fallback_deadline_mentions_possible_deadline(now) -> None:
    digest = fallback_digest(date(2026, 7, 7), [msg(text="нужно до 18:00", timestamp=now)])
    item = digest.direct_messages[0]

    assert item.important_context == "указан срок: до 18:00"
    assert item.deadline_text == "до 18:00"
    assert "Действий не требуется" not in item.action_items


def test_fallback_action_is_conservative_and_does_not_log_private_text(
    now,
    caplog,
) -> None:
    private_text = "пришли файл уникальный приватный маркер"
    with caplog.at_level(logging.DEBUG):
        digest = fallback_digest(date(2026, 7, 7), [msg(text=private_text, timestamp=now)])
    item = digest.direct_messages[0]

    assert item.action_items == "Открыть Telegram и ответить."
    assert "Действий не требуется" not in item.action_items
    assert private_text not in caplog.text


def test_fallback_extracts_request_readiness_flight_deadline_and_urgency(now) -> None:
    digest = fallback_digest(
        date(2026, 7, 7),
        [
            msg(message_id=1, text="ало ответь", timestamp=now),
            msg(message_id=2, text="завтра в 10 вылет ты готов?", timestamp=now),
            msg(message_id=3, text="срочно", timestamp=now),
        ],
    )
    item = digest.direct_messages[0]

    assert item.summary == (
        "Были сообщения с просьбой ответить и вопросом о готовности "
        "к вылету завтра в 10."
    )
    assert item.requests_to_me == "ответить; подтвердить готовность"
    assert "завтра в 10 вылет" in item.important_context
    assert "срочное" in item.important_context
    assert item.action_items == "Открыть Telegram и ответить."
    assert item.deadline_text == "завтра в 10"


def test_digest_cannot_drop_private_message_when_llm_omits_it(session) -> None:
    repository.save_message(session, msg(message_id=101, text="Обычное сообщение"))

    digest = generate_digest(session, OmittingLLM(), date(2026, 7, 7), "Europe/Moscow")

    assert digest.direct_messages
    item = digest.direct_messages[0]
    assert item.source_refs == [{"chat_id": "1", "message_id": 101}]
    assert item.chat == "Маша"
    assert item.summary == "Обычное сообщение"


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
        msg(
            chat_id="p1",
            chat_title="Маша",
            message_id=1,
            text="Обычное обновление",
            timestamp=now,
        ),
    )
    repository.save_message(
        session,
        msg(
            chat_id="p2",
            chat_title="Иван",
            message_id=1,
            text="Обычное сообщение",
            timestamp=now,
        ),
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
    assert email.sent[0][0] == "Telegram digest"


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


def test_legacy_keyless_digest_without_claims_is_cancelled_by_retry_scheduler(session) -> None:
    digest = DailyDigest(
        date="2026-07-07",
        direct_messages=[
            {
                "chat": "Allowed",
                "summary": "Allowed summary",
                "needs_reply": False,
                "source_refs": [{"chat_id": "123456789", "message_id": 1}],
            }
        ],
    )
    record = repository.save_digest(
        session,
        digest,
        "<p>saved html</p>",
        subject="saved subject",
        text="saved plain",
    )
    email = FakeEmail()

    sent = repository.retry_pending_digests(
        session,
        email,
        now=record.next_attempt_at,
        ignored_chat_ids=set(),
    )

    session.refresh(record)
    assert sent == 0
    assert email.sent == []
    assert record.email_status == "cancelled"
    assert record.subject == ""
    assert record.text_payload == ""
    assert record.html_payload == ""
    assert record.json_payload == ""
    assert repository.pending_digests(session) == []


def _legacy_keyless_digest_with_claim(
    session,
    *,
    message_id: int,
    is_outgoing: bool | None,
    payload_marker: str = "synthetic retry payload",
):
    repository.save_message(session, msg(message_id=message_id, text="synthetic source"))
    row = repository.get_message(session, "1", message_id)
    record, claimed_rows, created = repository.claim_digest_run_for_rows(
        session,
        digest_date="2026-07-07",
        rows=[row],
    )
    assert record is not None and created is True
    assert claimed_rows == [row]
    assert repository.update_digest_payload(
        session,
        record,
        DailyDigest(date="2026-07-07"),
        subject="[Telegram Detox][Digest] действия 3 срочное 1 — 2026-07-07",
        text=payload_marker,
        html=f"<p>{payload_marker}</p>",
    )
    record = repository.get_digest_record(session, record.id)
    row = repository.get_message(session, "1", message_id)
    record.digest_key = None
    row.is_outgoing = is_outgoing
    session.commit()
    return record, row


def test_legacy_keyless_digest_with_outgoing_claim_is_cancelled(session, caplog) -> None:
    marker = "synthetic-private-digest-marker"
    record, row = _legacy_keyless_digest_with_claim(
        session,
        message_id=901,
        is_outgoing=True,
        payload_marker=marker,
    )
    email = FakeEmail()

    sent = repository.retry_pending_digests(
        session,
        email,
        now=record.next_attempt_at,
        ignored_chat_ids=set(),
    )

    session.refresh(record)
    session.refresh(row)
    assert sent == 0
    assert email.sent == []
    assert record.email_status == "cancelled"
    assert record.subject == ""
    assert record.text_payload == ""
    assert record.html_payload == ""
    assert record.json_payload == ""
    assert record.claim_token is None
    assert record.claimed_at is None
    assert row.claimed_digest_id is None
    assert row.digested_at is None
    assert marker not in caplog.text


def test_claimed_legacy_keyless_digest_with_unknown_direction_is_cancelled(session) -> None:
    record, row = _legacy_keyless_digest_with_claim(
        session,
        message_id=902,
        is_outgoing=None,
    )
    claim_id = "synthetic-claim-id"
    claimed = repository.claim_pending_digest(
        session,
        record.id,
        record.next_attempt_at,
        claim_id,
    )
    assert claimed is not None
    email = FakeEmail()

    sent = repository.send_claimed_digest(
        session,
        record.id,
        claim_id,
        email,
        record.next_attempt_at,
    )

    session.refresh(record)
    session.refresh(row)
    assert sent is False
    assert email.sent == []
    assert record.email_status == "cancelled"
    assert record.text_payload == ""
    assert record.html_payload == ""
    assert record.json_payload == ""
    assert record.claim_token is None
    assert row.claimed_digest_id is None
    assert row.digested_at is None


def test_legacy_keyless_digest_with_incoming_claim_can_retry(session) -> None:
    record, row = _legacy_keyless_digest_with_claim(
        session,
        message_id=903,
        is_outgoing=False,
    )
    email = FakeEmail()

    sent = repository.retry_pending_digests(
        session,
        email,
        now=record.next_attempt_at,
        ignored_chat_ids=set(),
    )

    session.refresh(record)
    session.refresh(row)
    assert sent == 1
    assert len(email.sent) == 1
    assert email.sent[0][0] == "Telegram digest"
    for forbidden in (
        "[Telegram Detox]",
        "[Digest]",
        "действия",
        "срочное",
        "2026-07-07",
    ):
        assert forbidden not in email.sent[0][0]
    assert record.email_status == "sent"
    assert row.claimed_digest_id is None
    assert row.digested_at is not None


def test_successful_initial_digest_send_marks_digest_sent(session) -> None:
    repository.save_message(session, msg(message_id=313, text="ping"))
    email = FakeEmail()

    digest = send_daily_digest_pipeline(
        session,
        FakeLLM(),
        email,
        date(2026, 7, 7),
        "Europe/Moscow",
    )

    assert digest.email_status == "sent"
    assert repository.pending_digests(session) == []
    assert email.sent[0][0] == "Telegram digest"


def test_failed_initial_digest_send_sets_backoff_timestamp(session) -> None:
    repository.save_message(session, msg(message_id=314, text="ping"))

    send_daily_digest_pipeline(
        session,
        FakeLLM(),
        FakeEmail(fail=True),
        date(2026, 7, 7),
        "Europe/Moscow",
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
    )
    email = FakeEmail()
    record = repository.pending_digests(session)[0]

    sent = repository.retry_pending_digests(
        session,
        email,
        now=record.next_attempt_at,
        ignored_chat_ids=set(),
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
    )
    record = repository.pending_digests(session)[0]
    record.html_payload = "<p>ORIGINAL HTML</p>"
    session.commit()
    email = FakeEmail()

    repository.retry_pending_digests(
        session,
        email,
        now=record.next_attempt_at,
        ignored_chat_ids=set(),
    )

    assert email.sent[0][2] == "<p>ORIGINAL HTML</p>"


def test_pending_digest_retry_reuses_original_text(session) -> None:
    repository.save_message(session, msg(message_id=308, text="ping"))
    send_daily_digest_pipeline(
        session,
        FakeLLM(),
        FakeEmail(fail=True),
        date(2026, 7, 7),
        "Europe/Moscow",
    )
    record = repository.pending_digests(session)[0]
    record.text_payload = "ORIGINAL TEXT"
    session.commit()
    email = FakeEmail()

    repository.retry_pending_digests(
        session,
        email,
        now=record.next_attempt_at,
        ignored_chat_ids=set(),
    )

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
    )
    record = repository.pending_digests(session)[0]

    repository.retry_pending_digests(
        session,
        FakeEmail(),
        now=record.next_attempt_at,
        ignored_chat_ids=set(),
    )

    assert llm.calls == 1


def test_two_workers_cannot_claim_same_digest_job(session) -> None:
    repository.save_message(session, msg(message_id=310, text="ping"))
    send_daily_digest_pipeline(
        session,
        FakeLLM(),
        FakeEmail(fail=True),
        date(2026, 7, 7),
        "Europe/Moscow",
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


def test_valid_llm_digest_is_used_and_reports_safe_diagnostics(session, settings) -> None:
    from app.llm.client import HaikuClient
    from tests.test_llm_errors import (
        FakeClient,
        MalformedCompletions,
        digest_json,
        response_with,
    )

    repository.save_message(session, msg(message_id=314, text="ответь через час"))
    client = HaikuClient(settings)
    client.client = FakeClient(MalformedCompletions(response_with(digest_json())))

    digest = generate_digest(session, client, date(2026, 7, 7), "Europe/Moscow")

    assert digest.generated_by == "llm"
    assert digest.diagnostics.llm_attempted is True
    assert digest.diagnostics.llm_used is True
    assert digest.diagnostics.fallback_used is False
    assert digest.diagnostics.fallback_reason is None
    assert digest.diagnostics.chats_count == 1
    assert digest.diagnostics.messages_count == 1


def test_still_invalid_llm_digest_falls_back_with_safe_reason(
    session,
    settings,
    caplog,
) -> None:
    from app.llm.client import HaikuClient
    from tests.test_llm_errors import FakeClient, MalformedCompletions, response_with

    private_marker = "уникальный приватный текст для проверки логов"
    repository.save_message(session, msg(message_id=315, text=private_marker))
    client = HaikuClient(settings)
    client.client = FakeClient(
        MalformedCompletions(response_with('{"items":[{"chat_id":"1"}]}'))
    )

    with caplog.at_level(logging.INFO):
        digest = generate_digest(session, client, date(2026, 7, 7), "Europe/Moscow")

    assert digest.generated_by == "fallback"
    assert digest.diagnostics.llm_attempted is True
    assert digest.diagnostics.llm_used is False
    assert digest.diagnostics.fallback_used is True
    assert digest.diagnostics.fallback_reason == "validation_failed"
    assert digest.diagnostics.validation_error_type == "ValidationError"
    assert "items[0].summary" in digest.diagnostics.validation_error_paths
    assert "missing" in digest.diagnostics.validation_error_codes
    assert digest.diagnostics.repair_attempted is True
    assert digest.diagnostics.repair_used is False
    assert "validation_error_paths=items[0].summary" in caplog.text
    assert "validation_error_codes=missing" in caplog.text
    assert private_marker not in caplog.text


def test_digest_now_uses_persistent_delivery_pipeline() -> None:
    source = Path("app/cli/digest_now.py").read_text(encoding="utf-8")

    assert "send_daily_digest_pipeline" in source
    assert "send_and_store_digest" not in source


def test_digest_now_dry_run_does_not_send_email(settings, monkeypatch) -> None:
    from app.cli import digest_now

    session_factory = init_db(settings)
    with session_factory() as session:
        repository.save_message(session, msg(message_id=316, text="обычный тест"))
    monkeypatch.setattr(
        digest_now,
        "load_ignored_chats_from_settings",
        lambda _settings: type("Ignored", (), {"chat_ids": set()})(),
    )
    email = FakeEmail()
    output: list[str] = []

    digest = digest_now.run(
        dry_run=True,
        settings=settings,
        session_factory=session_factory,
        llm=FakeLLM(),
        email_sender=email,
        now=datetime.fromisoformat("2026-07-07T18:00:00+03:00"),
        output=output.append,
    )

    assert email.sent == []
    assert "Dry-run: digest would be generated" in output
    assert "Dry-run: digest not sent" in output
    assert "chats_count=1" in output
    assert "messages_count=1" in output
    assert "llm_used=true" in output
    assert digest.email_status == "pending"
    with session_factory() as session:
        assert repository.pending_digests(session) == []


def test_digest_now_uses_moscow_date_for_late_utc_timestamp(settings, monkeypatch) -> None:
    from app.cli import digest_now

    session_factory = init_db(settings)
    monkeypatch.setattr(
        digest_now,
        "load_ignored_chats_from_settings",
        lambda _settings: type("Ignored", (), {"chat_ids": set()})(),
    )

    digest = digest_now.run(
        dry_run=True,
        settings=settings,
        session_factory=session_factory,
        llm=FakeLLM(),
        now=datetime.fromisoformat("2026-07-19T22:30:00+00:00"),
        output=lambda _line: None,
    )

    assert digest.date == "2026-07-20"


def test_digest_now_dry_run_reports_fallback_reason(settings, monkeypatch) -> None:
    from app.cli import digest_now

    sensitive_field = "PRIVATE_TEXT_OR_TOKEN_secret_marker"

    class DiagnosticFailingLLM:
        def daily_digest(self, payload: dict) -> DailyDigest:
            raise LLMError(
                "invalid digest",
                reason_code="validation_failed",
                validation_error_type="ValidationError",
                validation_error_paths=[f"items[0].{sensitive_field}"],
                validation_error_codes=["extra_forbidden"],
                repair_attempted=True,
                repair_used=False,
                expected_chat_count=1,
                returned_chat_count=1,
                missing_chat_count=0,
                duplicate_chat_count=0,
                unknown_chat_count=0,
            )

    session_factory = init_db(settings)
    with session_factory() as session:
        repository.save_message(session, msg(message_id=317, text="обычный тест"))
    monkeypatch.setattr(
        digest_now,
        "load_ignored_chats_from_settings",
        lambda _settings: type("Ignored", (), {"chat_ids": set()})(),
    )
    output: list[str] = []

    digest_now.run(
        dry_run=True,
        llm_debug=True,
        settings=settings,
        session_factory=session_factory,
        llm=DiagnosticFailingLLM(),
        now=datetime.fromisoformat("2026-07-07T18:00:00+03:00"),
        output=output.append,
    )

    assert "llm_used=false" in output
    assert "fallback_reason=validation_failed" in output
    assert "validation_error_type=ValidationError" in output
    assert "validation_error_paths=items[0].<unknown_field>" in output
    assert "validation_error_codes=extra_forbidden" in output
    assert "repair_attempted=true" in output
    assert "repair_used=false" in output
    assert "expected_chat_count=1" in output
    assert "returned_chat_count=1" in output
    assert "missing_chat_count=0" in output
    assert "duplicate_chat_count=0" in output
    assert "unknown_chat_count=0" in output
    assert sensitive_field not in "\n".join(output)
    with session_factory() as session:
        assert repository.pending_digests(session) == []


def test_digest_now_normal_run_sends_email(settings, monkeypatch) -> None:
    from app.cli import digest_now

    session_factory = init_db(settings)
    with session_factory() as session:
        repository.save_message(session, msg(message_id=318, text="обычный тест"))
    monkeypatch.setattr(
        digest_now,
        "load_ignored_chats_from_settings",
        lambda _settings: type("Ignored", (), {"chat_ids": set()})(),
    )
    email = FakeEmail()
    output: list[str] = []

    digest = digest_now.run(
        settings=settings,
        session_factory=session_factory,
        llm=FakeLLM(),
        email_sender=email,
        now=datetime.fromisoformat("2026-07-07T18:00:00+03:00"),
        output=output.append,
    )

    assert len(email.sent) == 1
    assert digest.email_status == "sent"
    assert output == ["Digest sent."]


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
    assert digest.direct_messages[0].summary == "secret personal"


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

    assert "фото" in digest.group_updates[0].media_summary
    assert any(item.reason == "Возможно важное сообщение" for item in digest.review)


def test_non_ignored_channel_text_and_media_enter_digest(session, now) -> None:
    class CapturingChannelLLM(FakeLLM):
        def __init__(self) -> None:
            self.payload = None

        def daily_digest(self, payload: dict) -> DailyDigest:
            self.payload = payload
            return super().daily_digest(payload)

    repository.save_message(
        session,
        msg(
            chat_id="channel-allowed",
            chat_title="Учебный канал",
            chat_type=ChatType.channel,
            message_id=701,
            text="Расписание на завтра",
            timestamp=now,
        ),
    )
    repository.save_message(
        session,
        msg(
            chat_id="channel-allowed",
            chat_title="Учебный канал",
            chat_type=ChatType.channel,
            message_id=702,
            text=None,
            media_type=MediaType.video,
            timestamp=now,
        ),
    )
    repository.save_message(
        session,
        msg(
            chat_id="channel-ignored",
            chat_title="Игнорируемый канал",
            chat_type=ChatType.channel,
            message_id=703,
            text="Не включать",
            timestamp=now,
        ),
    )
    llm = CapturingChannelLLM()

    generate_digest(
        session,
        llm,
        date(2026, 7, 7),
        "Europe/Moscow",
        ignored_chat_ids={"channel-ignored"},
    )

    assert llm.payload is not None
    assert [chat["chat_id"] for chat in llm.payload["chats"]] == ["channel-allowed"]
    messages = llm.payload["chats"][0]["messages"]
    assert {item["message_id"] for item in messages} == {701, 702}
    assert any(item["media_type"] == MediaType.video.value for item in messages)


def test_private_known_p0_appears_only_in_urgent_section(session, now) -> None:
    message = msg(message_id=710, text="synthetic direct request", timestamp=now)
    repository.save_message(session, message)
    repository.mark_p0_classified(
        session,
        message.chat_id,
        message.message_id,
        P0Status.p0_strict.value,
        now,
        confidence=0.99,
    )

    digest = generate_digest(session, FakeLLM(), date(2026, 7, 7), "Europe/Moscow")
    output = render_plain_text(digest)

    assert len(digest.p0_alerts) == 1
    assert digest.direct_messages == []
    assert output.count("Synthetic") == 0
    urgent_part, private_part = output.split("ЛИЧНЫЕ СООБЩЕНИЯ", 1)
    assert "Маша" in urgent_part
    assert "Маша" not in private_part


def test_private_deterministic_p0_equivalent_is_urgent_without_persisted_status(
    session,
    now,
) -> None:
    repository.save_message(
        session,
        msg(
            message_id=709,
            text="завтра в 10 самолёт ты придёшь?",
            timestamp=now,
        ),
    )

    digest = generate_digest(session, FakeLLM(), date(2026, 7, 7), "Europe/Moscow")

    assert len(digest.p0_alerts) == 1
    assert digest.direct_messages == []


def test_group_reply_to_me_is_urgent_and_not_duplicated(session, now) -> None:
    repository.save_message(
        session,
        msg(
            chat_id="reply-group",
            chat_title="Synthetic group",
            chat_type=ChatType.group,
            message_id=708,
            text="synthetic reply",
            reply_to_message_id=1,
            reply_to_is_mine=True,
            timestamp=now,
        ),
    )

    digest = generate_digest(session, FakeLLM(), date(2026, 7, 7), "Europe/Moscow")

    assert len(digest.p0_alerts) == 1
    assert digest.group_updates == []


def test_ordinary_channel_deadline_stays_out_of_urgent(session, now) -> None:
    repository.save_message(
        session,
        msg(
            chat_id="channel-deadline",
            chat_title="Synthetic channel",
            chat_type=ChatType.channel,
            message_id=711,
            text="Дедлайн завтра в 10",
            timestamp=now,
        ),
    )

    digest = generate_digest(session, FakeLLM(), date(2026, 7, 7), "Europe/Moscow")

    assert digest.p0_alerts == []
    assert len(digest.channel_updates) == 1
    assert digest.group_updates == []


def test_channel_exact_mention_appears_only_in_urgent(session, now) -> None:
    repository.save_message(
        session,
        msg(
            chat_id="channel-mention",
            chat_title="Mention channel",
            chat_type=ChatType.channel,
            message_id=712,
            text="@fedocc посмотри",
            timestamp=now,
        ),
    )

    digest = generate_digest(
        session,
        FakeLLM(),
        date(2026, 7, 7),
        "Europe/Moscow",
        mention_usernames="fedocc",
    )

    assert len(digest.p0_alerts) == 1
    assert digest.p0_alerts[0].chat == "Mention channel"
    assert digest.channel_updates == []


def test_channel_rendering_keeps_facts_and_removes_genre_labels(session, now) -> None:
    class ChannelGenreLLM:
        def daily_digest(self, payload: dict) -> DailyDigest:
            source_refs = [
                message["source_ref"] for message in payload["chats"][0]["messages"]
            ]
            return DailyDigest(
                date=payload["date"],
                group_updates=[
                    {
                        "chat": "Synthetic channel",
                        "summary": (
                            "Канал с юмористическим контентом. "
                            "Есть объявление: квартира 116 кв.м., 150 тыс./мес."
                        ),
                        "source_refs": source_refs,
                    }
                ],
            )

    repository.save_message(
        session,
        msg(
            chat_id="channel-facts",
            chat_title="Synthetic channel",
            chat_type=ChatType.channel,
            message_id=713,
            text="Квартира 116 кв.м., 150 тыс./мес.",
            media_type=MediaType.photo,
            timestamp=now,
        ),
    )

    digest = generate_digest(
        session,
        ChannelGenreLLM(),
        date(2026, 7, 7),
        "Europe/Moscow",
    )
    output = (render_plain_text(digest) + render_html(digest)).casefold()

    assert "synthetic channel" in output
    assert "сообщений: 1" in output
    assert "1 фото" in output
    assert "время: 12:00 msk" in output
    assert "квартира 116 кв.м., 150 тыс./мес." in output
    for forbidden in (
        "канал с",
        "юмористическим",
        "философским",
        "развлекательным",
        "видеоконтентом",
    ):
        assert forbidden not in output


def test_channel_sanitizer_removes_only_broad_genre_wrappers() -> None:
    for broad_label in (
        "Канал с новостями",
        "Канал с философским контентом",
        "Канал с юмористическим контентом",
        "Канал с развлекательным контентом",
        "Канал с видеоконтентом",
        "Канал с объявлениями недвижимости",
    ):
        assert sanitize_channel_summary(broad_label) == ""

    summary = (
        "Канал с объявлениями недвижимости. "
        "Объявление недвижимости: квартира 116 кв.м., 150 тыс./мес."
    )
    assert sanitize_channel_summary(summary) == (
        "Объявление недвижимости: квартира 116 кв.м., 150 тыс./мес."
    )
    assert sanitize_channel_summary(
        "Канал с новостями: Опубликовано расписание на завтра."
    ) == "Опубликовано расписание на завтра."


def test_shallow_digest_is_marked_conservatively_for_manual_context(session) -> None:
    class ShallowLLM:
        def daily_digest(self, payload: dict) -> DailyDigest:
            return DailyDigest(
                date=payload["date"],
                direct_messages=[
                    {
                        "chat": "Маша",
                        "summary": "Есть сообщение.",
                        "needs_reply": False,
                        "source_refs": [payload["chats"][0]["messages"][0]["source_ref"]],
                    }
                ],
            )

    repository.save_message(session, msg(message_id=650, text="неполный ответ"))
    digest = generate_digest(session, ShallowLLM(), date(2026, 7, 7), "Europe/Moscow")
    output = render_plain_text(digest)

    assert digest.direct_messages[0].should_open_telegram is None
    assert "Открыть Telegram:" not in output
    assert "Действий нет" not in output


def test_same_chat_title_different_chat_ids_are_not_merged(session) -> None:
    repository.save_message(
        session,
        msg(chat_id="a", chat_title="Алексей", message_id=1, text="Обычное обновление"),
    )
    repository.save_message(
        session,
        msg(chat_id="b", chat_title="Алексей", message_id=1, text="Обычное сообщение"),
    )

    digest = generate_digest(session, FakeLLM(), date(2026, 7, 7), "Europe/Moscow")

    assert len(digest.direct_messages) == 2


def test_digest_repairs_count_only_summary(session) -> None:
    class CountOnlyLLM:
        def daily_digest(self, payload: dict) -> DailyDigest:
            return DailyDigest(
                date=payload["date"],
                direct_messages=[
                    {
                        "chat": "Маша",
                        "summary": "5 сообщений",
                        "what_happened": "5 сообщений",
                        "needs_reply": False,
                        "source_refs": [
                            payload["chats"][0]["messages"][0]["source_ref"]
                        ],
                    }
                ],
            )

    repository.save_message(session, msg(message_id=970, text="обычная переписка"))

    digest = generate_digest(
        session,
        CountOnlyLLM(),
        date(2026, 7, 7),
        "Europe/Moscow",
    )
    item = digest.direct_messages[0]

    assert item.summary == "Была обычная переписка без явного запроса."
    assert item.what_happened == "Была обычная переписка без явного запроса."
    assert "5 сообщений" not in render_plain_text(digest)


def test_digest_replaces_cross_chat_llm_item_with_one_item_per_chat(session) -> None:
    class CombinedChatsLLM:
        def daily_digest(self, payload: dict) -> DailyDigest:
            refs = [chat["messages"][0]["source_ref"] for chat in payload["chats"]]
            return DailyDigest(
                date=payload["date"],
                direct_messages=[
                    {
                        "chat": "Несколько чатов",
                        "summary": "Общее резюме двух переписок.",
                        "needs_reply": False,
                        "source_refs": refs,
                    }
                ],
            )

    repository.save_message(
        session,
        msg(chat_id="private-a", chat_title="Анна", message_id=1, text="первое"),
    )
    repository.save_message(
        session,
        msg(chat_id="private-b", chat_title="Борис", message_id=1, text="второе"),
    )

    digest = generate_digest(
        session,
        CombinedChatsLLM(),
        date(2026, 7, 7),
        "Europe/Moscow",
    )

    assert len(digest.direct_messages) == 2
    assert {item.chat for item in digest.direct_messages} == {"Анна", "Борис"}
    assert all(
        len({ref.chat_id for ref in item.source_refs}) == 1
        for item in digest.direct_messages
    )


def test_fallback_keeps_same_title_chats_separate() -> None:
    digest = fallback_digest(
        date(2026, 7, 7),
        [
            msg(chat_id="a", chat_title="Алексей", message_id=1),
            msg(chat_id="b", chat_title="Алексей", message_id=1),
        ],
    )

    assert len(digest.direct_messages) == 2


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


def test_startup_does_not_recover_old_daily_digests() -> None:
    source = Path("app/telegram/client.py").read_text(encoding="utf-8")

    assert "recover_missed_daily_digests" not in source


def test_pending_digest_prevents_manual_duplicate_send(session) -> None:
    repository.save_message(session, msg(message_id=803, text="pending"))
    email = FakeEmail(fail=True)
    first = send_daily_digest_pipeline(
        session,
        FakeLLM(),
        email,
        date(2026, 7, 7),
        "Europe/Moscow",
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


def test_two_pipeline_invocations_overlap_after_payload_persistence(settings, monkeypatch) -> None:
    factory = init_db(settings)
    with factory() as first_session:
        repository.save_message(first_session, msg(message_id=810, text="overlap payload"))
    email = MessageIdEmail()
    original_update = repository.update_digest_payload
    triggered = False

    def wrapped_update(session, record, digest, *, subject, text, html):
        nonlocal triggered
        updated = original_update(session, record, digest, subject=subject, text=text, html=html)
        if not triggered:
            triggered = True
            with factory() as second_session:
                send_daily_digest_pipeline(
                    second_session,
                    FakeLLM(),
                    email,
                    date(2026, 7, 7),
                    "Europe/Moscow",
                )
        return updated

    monkeypatch.setattr(repository, "update_digest_payload", wrapped_update)

    with factory() as first_session:
        send_daily_digest_pipeline(
            first_session,
            FakeLLM(),
            email,
            date(2026, 7, 7),
            "Europe/Moscow",
        )

    assert len(email.sent) == 1


def test_stale_building_payload_update_cannot_reset_sending_digest(settings) -> None:
    factory = init_db(settings)
    with factory() as seed_session:
        repository.save_message(seed_session, msg(message_id=814, text="stale building"))
        record, _, created = repository.claim_digest_run_for_rows(
            seed_session,
            digest_date="2026-07-07",
            rows=[repository.get_message(seed_session, "1", 814)],
        )
        assert created is True
        record_id = record.id
        delivery_id = record.delivery_id

    email = MessageIdEmail()
    digest = DailyDigest(date="2026-07-07")
    with factory() as worker_a, factory() as worker_b:
        stale_a = repository.get_digest_record(worker_a, record_id)
        stale_b = repository.get_digest_record(worker_b, record_id)
        assert stale_a and stale_b
        assert stale_a.email_status == stale_b.email_status == "building"

        assert repository.update_digest_payload(
            worker_a,
            stale_a,
            digest,
            subject="Telegram digest — 2026-07-07",
            text="digest body",
            html="<p>digest body</p>",
        )
        claimed = repository.claim_pending_digest(
            worker_a,
            record_id,
            datetime.now(UTC),
            "worker-a",
        )
        assert claimed is not None

        assert not repository.update_digest_payload(
            worker_b,
            stale_b,
            digest,
            subject="Telegram digest — 2026-07-07",
            text="second body",
            html="<p>second body</p>",
        )
        current = repository.get_digest_record(worker_b, record_id)
        assert current is not None
        assert current.email_status == "sending"
        assert current.delivery_id == delivery_id
        assert repository.claim_pending_digest(
            worker_b,
            record_id,
            datetime.now(UTC),
            "worker-b",
        ) is None

        assert repository.send_claimed_digest(
            worker_a,
            record_id,
            "worker-a",
            email,
            datetime.now(UTC),
        )

    with factory() as verify_session:
        record = repository.get_digest_record(verify_session, record_id)
        row = repository.get_message(verify_session, "1", 814)
        assert record is not None
        assert record.email_status == "sent"
        assert record.delivery_id == delivery_id
        assert row is not None and row.digested_at is not None
    assert len(email.sent) == 1
    assert email.message_ids == [delivery_id]


def test_two_pipeline_invocations_observing_building_send_once(settings, monkeypatch) -> None:
    factory = init_db(settings)
    with factory() as seed_session:
        repository.save_message(seed_session, msg(message_id=815, text="building overlap"))
        repository.claim_digest_run_for_rows(
            seed_session,
            digest_date="2026-07-07",
            rows=[repository.get_message(seed_session, "1", 815)],
        )

    email = MessageIdEmail()
    original_update = repository.update_digest_payload
    second_started = False

    def wrapped_update(session, record, digest, *, subject, text, html):
        nonlocal second_started
        if not second_started:
            second_started = True
            with factory() as second_session:
                send_daily_digest_pipeline(
                    second_session,
                    FakeLLM(),
                    email,
                    date(2026, 7, 7),
                    "Europe/Moscow",
                )
        return original_update(session, record, digest, subject=subject, text=text, html=html)

    monkeypatch.setattr(repository, "update_digest_payload", wrapped_update)
    with factory() as first_session:
        send_daily_digest_pipeline(
            first_session,
            FakeLLM(),
            email,
            date(2026, 7, 7),
            "Europe/Moscow",
        )

    assert len(email.sent) == 1


def test_pipeline_overlapping_with_retry_sends_once(settings, monkeypatch) -> None:
    factory = init_db(settings)
    with factory() as first_session:
        repository.save_message(first_session, msg(message_id=811, text="retry overlap"))
    email = MessageIdEmail()
    original_send_claimed = repository.send_claimed_digest
    triggered = False

    def wrapped_send_claimed(session, record_id, claim_token, email_sender, now):
        nonlocal triggered
        if not triggered:
            triggered = True
            with factory() as retry_session:
                repository.retry_pending_digests(
                    retry_session,
                    email_sender,
                    now,
                    ignored_chat_ids=set(),
                )
        return original_send_claimed(session, record_id, claim_token, email_sender, now)

    monkeypatch.setattr(repository, "send_claimed_digest", wrapped_send_claimed)

    with factory() as first_session:
        send_daily_digest_pipeline(
            first_session,
            FakeLLM(),
            email,
            date(2026, 7, 7),
            "Europe/Moscow",
        )

    assert len(email.sent) == 1


def test_pipeline_uses_claimed_digest_send_path(session, monkeypatch) -> None:
    repository.save_message(session, msg(message_id=812, text="send path"))
    original_send_claimed = repository.send_claimed_digest
    calls = 0

    def wrapped_send_claimed(session, record_id, claim_token, email_sender, now):
        nonlocal calls
        calls += 1
        return original_send_claimed(session, record_id, claim_token, email_sender, now)

    monkeypatch.setattr(repository, "send_claimed_digest", wrapped_send_claimed)

    send_daily_digest_pipeline(
        session,
        FakeLLM(),
        MessageIdEmail(),
        date(2026, 7, 7),
        "Europe/Moscow",
    )

    assert calls == 1


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


def test_building_digest_is_not_sendable(session) -> None:
    message = msg(message_id=807, text="building")
    repository.save_message(session, message)
    record, claimed_rows, created = repository.claim_digest_run_for_rows(
        session,
        digest_date="2026-07-07",
        rows=[repository.get_message(session, "1", 807)],
    )
    email = MessageIdEmail()

    sent = repository.retry_pending_digests(
        session,
        email,
        now=record.created_at,
        ignored_chat_ids=set(),
    )

    assert created is True
    assert claimed_rows
    assert record.email_status == "building"
    assert sent == 0
    assert email.sent == []
    assert repository.get_message(session, "1", 807).digested_at is None


def test_crash_after_digest_claim_before_payload_is_recovered_without_empty_send(session) -> None:
    message = msg(message_id=808, text="recover building")
    repository.save_message(session, message)
    record, _, _ = repository.claim_digest_run_for_rows(
        session,
        digest_date="2026-07-07",
        rows=[repository.get_message(session, "1", 808)],
    )
    assert record.email_status == "building"
    assert record.subject == ""
    email = MessageIdEmail()

    digest = send_daily_digest_pipeline(
        session,
        FakeLLM(),
        email,
        date(2026, 7, 7),
        "Europe/Moscow",
    )
    row = repository.get_message(session, "1", 808)

    assert len(email.sent) == 1
    assert email.sent[0][0]
    assert email.sent[0][1]
    assert email.sent[0][2]
    assert email.message_ids == [record.delivery_id]
    assert digest.email_status == "sent"
    assert row.digested_at is not None


def test_building_recovery_uses_claimed_digest_send_path(session, monkeypatch) -> None:
    message = msg(message_id=813, text="building send path")
    repository.save_message(session, message)
    repository.claim_digest_run_for_rows(
        session,
        digest_date="2026-07-07",
        rows=[repository.get_message(session, "1", 813)],
    )
    original_send_claimed = repository.send_claimed_digest
    calls = 0

    def wrapped_send_claimed(session, record_id, claim_token, email_sender, now):
        nonlocal calls
        calls += 1
        return original_send_claimed(session, record_id, claim_token, email_sender, now)

    monkeypatch.setattr(repository, "send_claimed_digest", wrapped_send_claimed)

    send_daily_digest_pipeline(
        session,
        FakeLLM(),
        MessageIdEmail(),
        date(2026, 7, 7),
        "Europe/Moscow",
    )

    assert calls == 1


def test_pending_digest_with_empty_payload_is_refused(session) -> None:
    message = msg(message_id=809, text="bad pending")
    repository.save_message(session, message)
    record, _, _ = repository.claim_digest_run_for_rows(
        session,
        digest_date="2026-07-07",
        rows=[repository.get_message(session, "1", 809)],
    )
    record.email_status = "pending"
    record.next_attempt_at = record.created_at
    session.commit()
    email = MessageIdEmail()

    sent = repository.retry_pending_digests(
        session,
        email,
        now=record.created_at,
        ignored_chat_ids=set(),
    )

    assert sent == 0
    assert email.sent == []
    assert repository.get_message(session, "1", 809).digested_at is None


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
    sent = repository.retry_pending_digests(
        session,
        retry_email,
        now=record.next_attempt_at,
        ignored_chat_ids=set(),
    )

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

    assert "Лимит: проанализировано 1 из 2 сообщений." in render_plain_text(digest)


def test_chat_limit_note_is_compact_and_summary_is_preserved(session) -> None:
    for index in range(1, 101):
        repository.save_message(
            session,
            msg(
                chat_id="limited-group",
                chat_title="Synthetic busy group",
                chat_type=ChatType.group,
                message_id=index,
                text=f"synthetic update {index}",
            ),
        )

    digest = generate_digest(
        session,
        FakeLLM(),
        date(2026, 7, 7),
        "Europe/Moscow",
        max_messages_per_chat=32,
    )
    output = render_plain_text(digest)

    assert "Synthetic busy group" in output
    assert "Суть:" in output
    assert "Лимит: проанализировано 32 из 100 сообщений." in output
    assert "часть сообщений не отправлена" not in output.casefold()


def test_urgent_large_private_and_group_chats_keep_compact_limit_notes(session) -> None:
    for chat_id, chat_title, chat_type in (
        ("limited-private", "Synthetic urgent private", ChatType.private),
        ("limited-urgent-group", "Synthetic urgent group", ChatType.group),
    ):
        for message_id in range(1, 101):
            repository.save_message(
                session,
                msg(
                    chat_id=chat_id,
                    chat_title=chat_title,
                    chat_type=chat_type,
                    message_id=message_id,
                    text=(
                        "@fedocc synthetic urgent request"
                        if message_id == 1
                        else f"synthetic update {message_id}"
                    ),
                ),
            )

    digest = generate_digest(
        session,
        FakeLLM(),
        date(2026, 7, 7),
        "Europe/Moscow",
        max_messages_per_chat=32,
        mention_usernames="fedocc",
    )
    output = render_plain_text(digest)

    assert digest.direct_messages == []
    assert digest.group_updates == []
    assert len(digest.p0_alerts) == 2
    assert all(item.message_count == 100 for item in digest.p0_alerts)
    assert all(item.analyzed_message_count == 32 for item in digest.p0_alerts)
    assert output.count("Лимит: проанализировано 32 из 100 сообщений.") == 2


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
