from __future__ import annotations

from app.db import repository
from app.email.sender import EmailSendError
from app.llm.client import LLMError
from app.models.schemas import P0Decision, P0Status
from app.services.p0 import SAFE_TEXT_LIMIT, handle_p0_candidate
from tests.fixtures.messages import msg


class FakeEmail:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.sent: list[tuple[str, str, str | None]] = []

    def send(self, subject: str, text: str, html: str | None = None, **kwargs) -> None:
        if self.fail:
            raise EmailSendError("smtp down")
        self.sent.append((subject, text, html))


class FakeLLM:
    def __init__(self, fail: bool = False, status: P0Status = P0Status.p0) -> None:
        self.fail = fail
        self.status = status

    def classify_p0(self, payload: dict) -> P0Decision:
        if self.fail:
            raise LLMError("down")
        return P0Decision(
            status=self.status,
            summary="Просит позвонить через час.",
            action="Позвонить.",
            confidence=0.9,
        )


def test_llm_failure_on_p0_candidate_sends_fallback_email(session) -> None:
    message = msg(text="Позвони через час")
    repository.save_message(session, message)
    email = FakeEmail()

    sent = handle_p0_candidate(session, message, FakeLLM(fail=True), email)

    assert sent is True
    assert len(email.sent) == 1
    assert email.sent[0][0].startswith("[ВОЗМОЖНО СРОЧНО]")


def test_duplicate_p0_does_not_send_second_email(session) -> None:
    message = msg(text="Позвони через час")
    repository.save_message(session, message)
    email = FakeEmail()

    assert handle_p0_candidate(session, message, FakeLLM(), email) is True
    assert handle_p0_candidate(session, message, FakeLLM(), email) is False

    assert len(email.sent) == 1


def test_non_candidate_does_not_call_email(session) -> None:
    message = msg(text="Просто мем")
    repository.save_message(session, message)
    email = FakeEmail()

    assert handle_p0_candidate(session, message, FakeLLM(status=P0Status.not_p0), email) is False
    assert email.sent == []


def test_private_message_llm_error_sends_immediate_fallback_email(session) -> None:
    message = msg(text="можешь посмотреть?")
    repository.save_message(session, message)
    email = FakeEmail()

    assert handle_p0_candidate(session, message, FakeLLM(fail=True), email) is True

    assert email.sent[0][0] == "[ПРОВЕРЬ] новое личное сообщение"


def test_p0_openai_error_sends_fallback_email(session) -> None:
    message = msg(text="можешь посмотреть?")
    repository.save_message(session, message)
    email = FakeEmail()

    assert handle_p0_candidate(session, message, FakeLLM(fail=True), email) is True
    assert email.sent


def test_private_message_review_sends_immediate_review_email(session) -> None:
    message = msg(text="есть вопрос")
    repository.save_message(session, message)
    email = FakeEmail()

    assert handle_p0_candidate(session, message, FakeLLM(status=P0Status.review), email) is True

    assert email.sent[0][0] == "[ПРОВЕРЬ] возможно важное личное сообщение"


def test_group_obvious_p0_llm_error_sends_fallback_email(session) -> None:
    message = msg(chat_type="group", chat_title="Лаба", text="ASAP дедлайн через 30 минут")
    repository.save_message(session, message)
    email = FakeEmail()

    assert handle_p0_candidate(session, message, FakeLLM(fail=True), email) is True

    assert email.sent[0][0].startswith("[ВОЗМОЖНО СРОЧНО]")


def test_p0_prefilter_handles_asap_deadline_variants() -> None:
    from app.services.prefilter import is_p0_candidate

    assert is_p0_candidate("ASAP")
    assert is_p0_candidate("as soon as possible")
    assert is_p0_candidate("до 18:30 сегодня")
    assert is_p0_candidate("deadline in 2 hours")


def test_p0_fallback_text_is_truncated(session) -> None:
    message = msg(text="Позвони " + ("x" * 2000))
    repository.save_message(session, message)
    email = FakeEmail()

    assert handle_p0_candidate(session, message, FakeLLM(fail=True), email) is True

    assert len(email.sent[0][1]) < SAFE_TEXT_LIMIT + 250
    assert "..." in email.sent[0][1]


def test_p0_smtp_failure_creates_pending_alert_job(session) -> None:
    message = msg(text="Позвони через час")
    repository.save_message(session, message)

    assert handle_p0_candidate(session, message, FakeLLM(fail=True), FakeEmail(fail=True)) is True

    jobs = repository.pending_alert_jobs(session)
    assert len(jobs) == 1
    assert jobs[0].status == "pending"


def test_pending_p0_alert_is_retried_and_marked_sent(session, now) -> None:
    message = msg(text="Позвони через час", timestamp=now)
    repository.save_message(session, message)
    handle_p0_candidate(session, message, FakeLLM(fail=True), FakeEmail(fail=True))

    sent = repository.retry_pending_alerts(session, FakeEmail(), now=now)

    assert sent == 1
    assert repository.pending_alert_jobs(session) == []


def test_pending_p0_alert_survives_restart(session) -> None:
    message = msg(text="Позвони через час")
    repository.save_message(session, message)
    handle_p0_candidate(session, message, FakeLLM(fail=True), FakeEmail(fail=True))

    assert repository.pending_alert_jobs(session)[0].chat_id == "1"


def test_p0_alert_deduplicated_by_chat_and_message_id(session) -> None:
    message = msg(text="Позвони через час")
    repository.save_message(session, message)
    email = FakeEmail(fail=True)

    handle_p0_candidate(session, message, FakeLLM(fail=True), email)
    handle_p0_candidate(session, message, FakeLLM(fail=True), email)

    assert len(repository.pending_alert_jobs(session)) == 1


def test_p0_retry_does_not_email_storm(session, now) -> None:
    message = msg(text="Позвони через час", timestamp=now)
    repository.save_message(session, message)
    handle_p0_candidate(session, message, FakeLLM(fail=True), FakeEmail(fail=True))
    email = FakeEmail(fail=True)

    assert repository.retry_pending_alerts(session, email, now=now) == 0
    assert len(email.sent) == 0


def test_p0_fallback_text_is_capped_and_escaped(session) -> None:
    message = msg(text="<script>" + ("x" * 2000))
    repository.save_message(session, message)
    email = FakeEmail()

    handle_p0_candidate(session, message, FakeLLM(fail=True), email)

    assert "<script>" in email.sent[0][1]
    assert "&lt;script&gt;" in email.sent[0][2]
    assert len(email.sent[0][1]) < SAFE_TEXT_LIMIT + 250


def test_p0_message_is_deduplicated(session) -> None:
    message = msg(text="есть вопрос")
    repository.save_message(session, message)
    email = FakeEmail()

    assert handle_p0_candidate(session, message, FakeLLM(status=P0Status.review), email) is True
    assert handle_p0_candidate(session, message, FakeLLM(status=P0Status.review), email) is False

    assert len(email.sent) == 1
