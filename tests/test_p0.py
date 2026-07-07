from __future__ import annotations

from app.db import repository
from app.llm.client import LLMError
from app.models.schemas import P0Decision, P0Status
from app.services.p0 import SAFE_TEXT_LIMIT, handle_p0_candidate
from tests.fixtures.messages import msg


class FakeEmail:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str | None]] = []

    def send(self, subject: str, text: str, html: str | None = None) -> None:
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


def test_p0_message_is_deduplicated(session) -> None:
    message = msg(text="есть вопрос")
    repository.save_message(session, message)
    email = FakeEmail()

    assert handle_p0_candidate(session, message, FakeLLM(status=P0Status.review), email) is True
    assert handle_p0_candidate(session, message, FakeLLM(status=P0Status.review), email) is False

    assert len(email.sent) == 1
