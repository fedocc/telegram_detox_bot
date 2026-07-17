from __future__ import annotations

from datetime import date, timedelta

from app.cli.cleanup import run as run_cleanup_cli
from app.db import repository
from app.db.session import init_db
from app.db.tables import DigestRecord
from app.email.sender import EmailSendError
from app.llm.client import LLMError
from app.models.schemas import DailyDigest
from app.services.maintenance import run_cleanup, run_daily_job
from tests.fixtures.messages import msg


class FailingLLM:
    def daily_digest(self, payload: dict) -> DailyDigest:
        raise LLMError("down")


class FailingEmail:
    def send(self, subject: str, text: str, html: str | None = None) -> None:
        raise EmailSendError("smtp down")


def test_cleanup_runs_when_digest_generation_fails(session, now) -> None:
    old = now - timedelta(days=20)
    repository.save_message(session, msg(message_id=1, timestamp=old))

    run_daily_job(
        session,
        FailingLLM(),
        FailingEmail(),
        date(2026, 7, 7),
        "Europe/Moscow",
        raw_retention_days=14,
        digest_retention_days=90,
        now=now,
    )

    assert repository.get_message(session, "1", 1) is None


def test_cleanup_runs_when_email_fails(session, now) -> None:
    old = now - timedelta(days=20)
    repository.save_message(session, msg(message_id=2, timestamp=old))

    run_daily_job(
        session,
        FailingLLM(),
        FailingEmail(),
        date(2026, 7, 7),
        "Europe/Moscow",
        raw_retention_days=14,
        digest_retention_days=90,
        now=now,
    )

    assert repository.get_message(session, "1", 2) is None


def test_cleanup_deletes_raw_messages_after_14_days(session, now) -> None:
    repository.save_message(session, msg(message_id=3, timestamp=now - timedelta(days=15)))
    repository.save_message(session, msg(message_id=4, timestamp=now - timedelta(days=13)))

    raw, _ = run_cleanup(session, 14, 90, now)

    assert raw == 1
    assert repository.get_message(session, "1", 3) is None
    assert repository.get_message(session, "1", 4) is not None


def test_cleanup_removes_old_processed_messages(session, now) -> None:
    old_message = msg(message_id=30, timestamp=now - timedelta(days=15), text="old private")
    repository.save_message(session, old_message)
    repository.mark_messages_digested(session, [old_message], now - timedelta(days=14))

    raw, _ = run_cleanup(session, 14, 90, now)

    assert raw == 1
    assert repository.get_message(session, "1", 30) is None


def test_cleanup_keeps_digests_for_90_days(session, now) -> None:
    session.add(
        DigestRecord(
            digest_date="2026-01-01",
            created_at=now - timedelta(days=91),
            json_payload="{}",
            html_payload="",
            generated_by="fallback",
            email_status="sent",
        )
    )
    session.add(
        DigestRecord(
            digest_date="2026-07-01",
            created_at=now - timedelta(days=89),
            json_payload="{}",
            html_payload="",
            generated_by="fallback",
            email_status="sent",
        )
    )
    session.commit()

    _, digests = run_cleanup(session, 14, 90, now)

    assert digests == 1
    assert len(session.query(DigestRecord).all()) == 1


def test_cleanup_cli_cancels_unsafe_alerts_idempotently_without_private_text(settings, now) -> None:
    factory = init_db(settings)
    with factory() as session:
        message = msg(chat_id="legacy-cleanup", message_id=1, text="private text must not print")
        repository.save_message(session, message)
        repository.mark_p0_classified(
            session,
            "legacy-cleanup",
            1,
            "P0",
            now,
            confidence=0.99,
        )
        repository.create_alert_job(
            session,
            chat_id="legacy-cleanup",
            message_id=1,
            alert_type="p0",
            subject="legacy",
            text_body="private text must not print",
            html_body="<p>private text must not print</p>",
            now=now,
        )

    output: list[str] = []
    first = run_cleanup_cli(settings, output=output.append)
    second = run_cleanup_cli(settings, output=output.append)

    assert first[0] == 1
    assert second[0] == 0
    assert "private text must not print" not in "\n".join(output)
