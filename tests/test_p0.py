from __future__ import annotations

from app.db import repository
from app.llm.client import LLMError
from app.models.schemas import P0Decision
from app.services.p0 import handle_p0_candidate
from tests.fixtures.messages import msg


class FakeEmail:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str | None]] = []

    def send(self, subject: str, text: str, html: str | None = None) -> None:
        self.sent.append((subject, text, html))


class FakeLLM:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail

    def classify_p0(self, payload: dict) -> P0Decision:
        if self.fail:
            raise LLMError("down")
        return P0Decision(
            is_p0=True,
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

    assert handle_p0_candidate(session, message, FakeLLM(), email) is False
    assert email.sent == []
