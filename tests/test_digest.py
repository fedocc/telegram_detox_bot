from __future__ import annotations

from datetime import date

from app.db import repository
from app.email.render import render_html
from app.models.schemas import ChatType, DailyDigest, DigestNoiseCount, MediaType
from app.services.digest import fallback_digest, generate_digest
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
