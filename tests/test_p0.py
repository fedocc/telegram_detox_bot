from __future__ import annotations

import pytest

from app.db import repository
from app.email.sender import EmailSendError
from app.llm.client import LLMError
from app.models.schemas import ChatType, MediaType, P0Decision, P0Status
from app.services.p0 import handle_p0_candidate
from tests.fixtures.messages import msg


class FakeEmail:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.sent: list[tuple[str, str, str | None]] = []

    def send(self, subject: str, text: str, html: str | None = None, **kwargs) -> None:
        if self.fail:
            raise EmailSendError("smtp down")
        self.sent.append((subject, text, html))


class FailingGmailApiEmail(FakeEmail):
    def send(self, subject: str, text: str, html: str | None = None, **kwargs) -> None:
        raise EmailSendError("Gmail API send failed")


class FakeLLM:
    def __init__(
        self,
        fail: bool = False,
        status: P0Status = P0Status.p0,
        deadline_text: str | None = None,
        confidence: float = 0.9,
        summary: str = "Сервер недоступен; требуется срочный звонок инженеру.",
        reason: str | None = None,
        action: str = "Позвонить инженеру.",
    ) -> None:
        self.fail = fail
        self.status = status
        self.deadline_text = deadline_text
        self.confidence = confidence
        self.summary = summary
        self.reason = reason
        self.action = action
        self.calls = 0
        self.payloads: list[dict] = []

    def classify_p0(self, payload: dict) -> P0Decision:
        self.calls += 1
        self.payloads.append(payload)
        if self.fail:
            raise LLMError("down")
        return P0Decision(
            status=self.status,
            summary=self.summary,
            reason=self.reason,
            action=self.action,
            deadline_text=self.deadline_text,
            confidence=self.confidence,
        )


def test_clear_private_call_request_survives_llm_failure(session) -> None:
    message = msg(text="Позвони через час")
    repository.save_message(session, message)
    email = FakeEmail()

    sent = handle_p0_candidate(session, message, FakeLLM(fail=True), email)

    assert sent is True
    assert len(email.sent) == 1
    assert "Позвони через час" in email.sent[0][1]
    assert repository.get_message(session, "1", 1).p0_classification == "P0_STRICT"


def test_duplicate_p0_does_not_send_second_email(session) -> None:
    message = msg(text="Позвони через час: сервер недоступен")
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


def test_random_private_text_does_not_send_email(session) -> None:
    message = msg(text="смотри какой смешной ролик")
    repository.save_message(session, message)
    email = FakeEmail()

    assert handle_p0_candidate(session, message, FakeLLM(status=P0Status.not_p0), email) is False
    assert email.sent == []
    assert repository.get_message(session, "1", 1).p0_classification == "NOT_P0"
    assert repository.pending_alert_jobs(session) == []


def test_private_kak_dela_does_not_send_email(session) -> None:
    message = msg(text="как дела")
    repository.save_message(session, message)
    email = FakeEmail()

    assert handle_p0_candidate(
        session,
        message,
        FakeLLM(status=P0Status.p0_strict, confidence=0.99),
        email,
    ) is False
    assert email.sent == []
    assert repository.pending_alert_jobs(session) == []


def test_private_boring_test_text_does_not_send_email(session) -> None:
    message = msg(text="бла бла просто тест")
    repository.save_message(session, message)
    email = FakeEmail()

    assert handle_p0_candidate(
        session,
        message,
        FakeLLM(status=P0Status.p0_strict, confidence=0.99),
        email,
    ) is False
    assert email.sent == []
    assert repository.pending_alert_jobs(session) == []


@pytest.mark.parametrize("status", [P0Status.not_p0, P0Status.p0_strict])
def test_private_past_tense_watch_statement_does_not_email(session, status) -> None:
    message = msg(text="я посмотрел фильм вчера")
    repository.save_message(session, message)
    email = FakeEmail()

    assert handle_p0_candidate(
        session,
        message,
        FakeLLM(status=status, confidence=0.99),
        email,
    ) is False
    assert email.sent == []
    assert repository.pending_alert_jobs(session) == []


def test_private_answer_noun_statement_does_not_email_for_strict_llm(session) -> None:
    message = msg(text="этот ответ был правильным")
    repository.save_message(session, message)
    email = FakeEmail()

    assert handle_p0_candidate(
        session,
        message,
        FakeLLM(status=P0Status.p0_strict, confidence=0.99),
        email,
    ) is False
    assert email.sent == []
    assert repository.pending_alert_jobs(session) == []


def test_private_time_only_statement_does_not_email_for_strict_llm(session) -> None:
    message = msg(text="сегодня хорошая погода")
    repository.save_message(session, message)
    email = FakeEmail()

    assert handle_p0_candidate(
        session,
        message,
        FakeLLM(status=P0Status.p0_strict, confidence=0.99),
        email,
    ) is False
    assert email.sent == []
    assert repository.pending_alert_jobs(session) == []


def test_trusted_private_time_only_statement_does_not_email(session, settings) -> None:
    settings.p0_trusted_sender_ids = "42"
    message = msg(text="сегодня хорошая погода")
    repository.save_message(session, message)
    email = FakeEmail()

    assert handle_p0_candidate(
        session,
        message,
        FakeLLM(status=P0Status.p0_strict, confidence=0.99),
        email,
        settings=settings,
    ) is False
    assert email.sent == []
    assert repository.pending_alert_jobs(session) == []


def test_clear_private_check_request_overrides_low_llm_confidence(session) -> None:
    message = msg(text="можешь потом посмотреть?")
    repository.save_message(session, message)
    email = FakeEmail()

    assert handle_p0_candidate(
        session,
        message,
        FakeLLM(status=P0Status.p0, confidence=0.4),
        email,
    ) is True
    assert len(email.sent) == 1


def test_private_check_request_uses_deterministic_fallback_on_llm_error(session) -> None:
    message = msg(text="можешь посмотреть?")
    repository.save_message(session, message)
    email = FakeEmail()

    assert handle_p0_candidate(session, message, FakeLLM(fail=True), email) is True
    assert len(email.sent) == 1


def test_private_check_request_uses_deterministic_fallback_on_provider_error(session) -> None:
    message = msg(text="можешь посмотреть?")
    repository.save_message(session, message)
    email = FakeEmail()

    assert handle_p0_candidate(session, message, FakeLLM(fail=True), email) is True
    assert len(email.sent) == 1


def test_private_message_review_stays_in_digest(session) -> None:
    message = msg(text="есть вопрос")
    repository.save_message(session, message)
    email = FakeEmail()

    assert handle_p0_candidate(session, message, FakeLLM(status=P0Status.review), email) is False
    assert email.sent == []


def test_group_deadline_and_urgency_survive_llm_error(session) -> None:
    message = msg(chat_type="group", chat_title="Лаба", text="ASAP дедлайн через 30 минут")
    repository.save_message(session, message)
    email = FakeEmail()

    assert handle_p0_candidate(session, message, FakeLLM(fail=True), email) is True
    assert len(email.sent) == 1


def test_p0_prefilter_handles_asap_deadline_variants() -> None:
    from app.services.prefilter import is_p0_candidate

    assert is_p0_candidate("ASAP")
    assert is_p0_candidate("as soon as possible")
    assert is_p0_candidate("до 18:30 сегодня")
    assert is_p0_candidate("deadline in 2 hours")


def test_deterministic_private_email_keeps_full_original_text(session) -> None:
    message = msg(text="Позвони " + ("x" * 2000))
    repository.save_message(session, message)
    email = FakeEmail()

    assert handle_p0_candidate(session, message, FakeLLM(fail=True), email) is True
    assert message.text in email.sent[0][1]


def test_p0_smtp_failure_creates_pending_alert_job(session) -> None:
    message = msg(text="Позвони через час: сервер недоступен")
    repository.save_message(session, message)

    assert handle_p0_candidate(session, message, FakeLLM(), FakeEmail(fail=True)) is True

    jobs = repository.pending_alert_jobs(session)
    assert len(jobs) == 1
    assert jobs[0].status == "pending"


def test_pending_p0_retry_works_when_gmail_api_send_fails(session) -> None:
    message = msg(text="Позвони через час: сервер недоступен")
    repository.save_message(session, message)

    assert handle_p0_candidate(session, message, FakeLLM(), FailingGmailApiEmail())

    jobs = repository.pending_alert_jobs(session)
    assert len(jobs) == 1
    assert jobs[0].last_error_safe == "EmailSendError"


def test_pending_p0_alert_is_retried_and_marked_sent(session, now) -> None:
    message = msg(text="Позвони через час: сервер недоступен", timestamp=now)
    repository.save_message(session, message)
    handle_p0_candidate(session, message, FakeLLM(), FakeEmail(fail=True))

    job = repository.pending_alert_jobs(session)[0]
    sent = repository.retry_pending_alerts(session, FakeEmail(), now=job.next_attempt_at)

    assert sent == 1
    assert repository.pending_alert_jobs(session) == []


def test_pending_p0_alert_survives_restart(session) -> None:
    message = msg(text="Позвони через час: сервер недоступен")
    repository.save_message(session, message)
    handle_p0_candidate(session, message, FakeLLM(), FakeEmail(fail=True))

    assert repository.pending_alert_jobs(session)[0].chat_id == "1"


def test_p0_alert_deduplicated_by_chat_and_message_id(session) -> None:
    message = msg(text="Позвони через час: сервер недоступен")
    repository.save_message(session, message)
    email = FakeEmail(fail=True)

    handle_p0_candidate(session, message, FakeLLM(), email)
    handle_p0_candidate(session, message, FakeLLM(), email)

    assert len(repository.pending_alert_jobs(session)) == 1


def test_p0_retry_does_not_email_storm(session, now) -> None:
    message = msg(text="Позвони через час: сервер недоступен", timestamp=now)
    repository.save_message(session, message)
    handle_p0_candidate(session, message, FakeLLM(), FakeEmail(fail=True))
    email = FakeEmail(fail=True)

    assert repository.retry_pending_alerts(session, email, now=now) == 0
    assert len(email.sent) == 0


def test_first_failed_alert_waits_one_minute_before_retry(session, now) -> None:
    message = msg(text="Позвони через час: сервер недоступен", timestamp=now)
    repository.save_message(session, message)

    handle_p0_candidate(session, message, FakeLLM(), FakeEmail(fail=True))
    job = repository.pending_alert_jobs(session)[0]

    assert job.attempts == 1
    assert job.next_attempt_at > now.replace(tzinfo=None)
    assert repository.retry_pending_alerts(session, FakeEmail(), now=now) == 0


def test_retry_respects_next_attempt_at(session, now) -> None:
    message = msg(text="Позвони через час: сервер недоступен", timestamp=now)
    repository.save_message(session, message)
    handle_p0_candidate(session, message, FakeLLM(), FakeEmail(fail=True))
    email = FakeEmail()

    assert repository.retry_pending_alerts(session, email, now=now) == 0
    assert email.sent == []


def test_backoff_caps_at_sixty_minutes(session, now) -> None:
    from datetime import timedelta

    message = msg(text="Позвони через час: сервер недоступен", timestamp=now)
    repository.save_message(session, message)
    handle_p0_candidate(session, message, FakeLLM(), FakeEmail(fail=True))
    job = repository.pending_alert_jobs(session)[0]
    for _ in range(5):
        repository.retry_pending_alerts(session, FakeEmail(fail=True), now=job.next_attempt_at)
        job = repository.pending_alert_jobs(session)[0]

    assert job.next_attempt_at <= now.replace(tzinfo=None) + timedelta(
        minutes=1 + 5 + 15 + 60 + 60 + 60
    )


def test_two_workers_cannot_claim_same_alert_job(session, now) -> None:
    message = msg(text="Позвони через час: сервер недоступен", timestamp=now)
    repository.save_message(session, message)
    handle_p0_candidate(session, message, FakeLLM(), FakeEmail(fail=True))
    job = repository.pending_alert_jobs(session)[0]

    first = repository.claim_pending_alert(session, job.id, job.next_attempt_at, "token-1")
    second = repository.claim_pending_alert(session, job.id, job.next_attempt_at, "token-2")

    assert first is not None
    assert second is None


def test_stale_sending_alert_becomes_retryable(session, now) -> None:
    message = msg(text="Позвони через час: сервер недоступен", timestamp=now)
    repository.save_message(session, message)
    handle_p0_candidate(session, message, FakeLLM(), FakeEmail(fail=True))
    job = repository.pending_alert_jobs(session)[0]
    repository.claim_pending_alert(session, job.id, job.next_attempt_at, "token-1")

    repository.release_stale_alert_claims(session, job.next_attempt_at, stale_minutes=0)

    assert repository.pending_alert_jobs(session)


def test_claimed_job_is_not_sent_by_second_worker(session, now) -> None:
    message = msg(text="Позвони через час: сервер недоступен", timestamp=now)
    repository.save_message(session, message)
    handle_p0_candidate(session, message, FakeLLM(), FakeEmail(fail=True))
    job = repository.pending_alert_jobs(session)[0]
    repository.claim_pending_alert(session, job.id, job.next_attempt_at, "token-1")
    email = FakeEmail()

    assert (
        repository.send_claimed_alert(session, job.id, "token-2", email, job.next_attempt_at)
        is False
    )
    assert email.sent == []


def test_clear_private_request_survives_malformed_provider_response(session, settings) -> None:
    from app.llm.client import HaikuClient
    from tests.test_llm_errors import FakeClient, MalformedCompletions

    message = msg(text="можешь посмотреть?")
    repository.save_message(session, message)
    client = HaikuClient(settings)
    client.client = FakeClient(MalformedCompletions(type("Response", (), {"choices": []})()))
    email = FakeEmail()

    assert handle_p0_candidate(session, message, client, email) is True
    assert len(email.sent) == 1


def test_clear_private_request_survives_real_openai_error(session, settings) -> None:
    from app.llm.client import HaikuClient
    from tests.test_llm_errors import BrokenClient

    message = msg(text="можешь посмотреть?")
    repository.save_message(session, message)
    client = HaikuClient(settings)
    client.client = BrokenClient()
    email = FakeEmail()

    assert handle_p0_candidate(session, message, client, email) is True
    assert len(email.sent) == 1


def test_p0_llm_error_does_not_create_email_body(session) -> None:
    message = msg(text="<script>" + ("x" * 2000))
    repository.save_message(session, message)
    email = FakeEmail()

    assert handle_p0_candidate(session, message, FakeLLM(fail=True), email) is False
    assert email.sent == []


def test_p0_message_is_deduplicated(session) -> None:
    message = msg(text="есть вопрос")
    repository.save_message(session, message)
    email = FakeEmail()

    assert handle_p0_candidate(session, message, FakeLLM(status=P0Status.review), email) is False
    assert handle_p0_candidate(session, message, FakeLLM(status=P0Status.review), email) is False

    assert email.sent == []


def test_call_back_in_one_hour_is_p0() -> None:
    from app.services.prefilter import is_urgent_call_candidate

    assert is_urgent_call_candidate("Please call me back in one hour")


def test_pozvoni_cherez_chas_is_p0() -> None:
    from app.services.prefilter import is_urgent_call_candidate

    assert is_urgent_call_candidate("Позвони через час")


def test_join_call_in_thirty_minutes_is_p0() -> None:
    from app.services.prefilter import is_urgent_call_candidate

    assert is_urgent_call_candidate("Please join the call in 30 minutes")


def test_phone_arrives_today_is_not_an_urgent_call_candidate() -> None:
    from app.services.prefilter import is_urgent_call_candidate

    assert not is_urgent_call_candidate("My new phone arrives today")


def test_private_call_request_is_p0_even_without_same_day_urgency(session) -> None:
    message = msg(text="Can we call tomorrow?")
    repository.save_message(session, message)
    email = FakeEmail()

    assert handle_p0_candidate(session, message, FakeLLM(status=P0Status.not_p0), email) is True
    assert len(email.sent) == 1


def test_deterministic_private_call_overrides_llm_not_p0(session) -> None:
    message = msg(text="Please call me back in one hour")
    repository.save_message(session, message)
    email = FakeEmail()

    assert handle_p0_candidate(session, message, FakeLLM(status=P0Status.not_p0), email) is True
    assert len(email.sent) == 1


def test_phone_arrives_today_not_p0_does_not_send_email(session) -> None:
    message = msg(text="My new phone arrives today")
    repository.save_message(session, message)
    email = FakeEmail()

    assert handle_p0_candidate(
        session,
        message,
        FakeLLM(status=P0Status.not_p0, confidence=0.99),
        email,
    ) is False
    assert email.sent == []


def test_private_call_today_sends_email(session) -> None:
    message = msg(text="Call me today if you can")
    repository.save_message(session, message)
    email = FakeEmail()

    assert handle_p0_candidate(session, message, FakeLLM(status=P0Status.not_p0), email) is True
    assert len(email.sent) == 1


def test_private_urgent_response_request_sends_email(session) -> None:
    message = msg(text="срочно ответь, нужен ответ сегодня")
    repository.save_message(session, message)
    email = FakeEmail()

    assert handle_p0_candidate(
        session, message, FakeLLM(status=P0Status.p0, confidence=0.95), email
    )
    assert len(email.sent) == 1
    assert repository.get_message(session, "1", 1).p0_classification == "P0_STRICT"


def test_private_reply_urgent_now_sends_email(session) -> None:
    message = msg(text="ответь срочно сейчас об этом")
    repository.save_message(session, message)
    llm = FakeLLM(status=P0Status.p0_strict, confidence=0.99)
    email = FakeEmail()

    assert handle_p0_candidate(session, message, llm, email) is True
    assert len(email.sent) == 1
    assert repository.get_message(session, "1", 1).p0_classification == "P0_STRICT"


def test_private_can_you_answer_today_sends_email(session) -> None:
    message = msg(text="можешь сегодня ответить?")
    repository.save_message(session, message)
    email = FakeEmail()

    assert handle_p0_candidate(
        session,
        message,
        FakeLLM(status=P0Status.not_p0),
        email,
    ) is True
    assert len(email.sent) == 1
    assert repository.get_message(session, "1", 1).p0_classification == "P0_STRICT"


def test_private_important_question_now_sends_email(session) -> None:
    message = msg(text="срочно ответь по важному вопросу сейчас")
    repository.save_message(session, message)
    llm = FakeLLM(status=P0Status.p0_strict, confidence=0.99)
    email = FakeEmail()

    assert handle_p0_candidate(session, message, llm, email) is True
    assert len(email.sent) == 1
    assert repository.get_message(session, "1", 1).p0_classification == "P0_STRICT"


def test_trusted_private_urgent_request_sends_email(session, settings) -> None:
    settings.p0_trusted_sender_ids = "42"
    message = msg(text="срочно ответь по важному вопросу")
    repository.save_message(session, message)
    llm = FakeLLM(status=P0Status.p0_strict, confidence=0.99)
    email = FakeEmail()

    assert handle_p0_candidate(session, message, llm, email, settings=settings) is True
    assert len(email.sent) == 1
    assert repository.get_message(session, "1", 1).p0_classification == "P0_STRICT"


def test_bare_private_srochno_sends_email(session) -> None:
    message = msg(text="срочно")
    repository.save_message(session, message)
    llm = FakeLLM()
    email = FakeEmail()

    assert handle_p0_candidate(session, message, llm, email) is True
    assert llm.calls == 1
    assert len(email.sent) == 1
    assert repository.get_message(session, "1", 1).p0_classification == "P0_STRICT"


def test_private_otvet_srochno_sends_email(session) -> None:
    message = msg(text="ответь срочно")
    repository.save_message(session, message)
    llm = FakeLLM()
    email = FakeEmail()

    assert handle_p0_candidate(session, message, llm, email) is True
    assert llm.calls == 1
    assert len(email.sent) == 1
    assert repository.get_message(session, "1", 1).p0_classification == "P0_STRICT"


def test_private_urgent_reply_please_sends_email(session) -> None:
    message = msg(text="urgent reply please now about this")
    repository.save_message(session, message)
    llm = FakeLLM()
    email = FakeEmail()

    assert handle_p0_candidate(session, message, llm, email) is True
    assert llm.calls == 1
    assert len(email.sent) == 1
    assert repository.get_message(session, "1", 1).p0_classification == "P0_STRICT"


def test_private_call_me_now_sends_email(session) -> None:
    message = msg(text="позвони мне сейчас")
    repository.save_message(session, message)
    email = FakeEmail()

    assert handle_p0_candidate(
        session,
        message,
        FakeLLM(status=P0Status.p0_candidate, confidence=0.3),
        email,
    ) is True
    assert len(email.sent) == 1
    assert repository.get_message(session, "1", 1).p0_classification == "P0_STRICT"


def test_private_routing_does_not_reject_vague_llm_wording(session) -> None:
    message = msg(text="Ответь по договору сегодня до 18:00")
    repository.save_message(session, message)
    llm = FakeLLM(
        summary="Urgent request to respond to an unspecified urgent matter",
        action="Respond immediately to clarify the urgent issue",
        confidence=0.99,
    )
    email = FakeEmail()

    assert handle_p0_candidate(session, message, llm, email) is True
    assert len(email.sent) == 1
    assert repository.get_message(session, "1", 1).p0_classification == "P0_STRICT"


def test_contract_file_deadline_sends_strict_email_with_raw_text(session) -> None:
    raw_text = "Федя, срочно пришли файл договора до 18:00"
    message = msg(text=raw_text)
    repository.save_message(session, message)
    llm = FakeLLM(
        summary="Для сделки нужен файл договора до 18:00.",
        action="Прислать файл договора.",
        deadline_text="до 18:00",
        confidence=0.85,
    )
    email = FakeEmail()

    assert handle_p0_candidate(session, message, llm, email) is True
    assert len(email.sent) == 1
    body = email.sent[0][1]
    assert raw_text in body
    assert "Чат: Маша" in body
    assert "Отправитель: Sender" in body
    assert "Время: 2026-07-07T12:00:00+03:00" in body
    assert "Почему P0_STRICT:" in body
    assert "Конкретное действие: Прислать файл договора." in body
    assert "Дедлайн: до 18:00" in body
    assert repository.get_message(session, "1", 1).p0_classification == "P0_STRICT"


def test_deterministic_private_request_overrides_low_confidence(session) -> None:
    message = msg(text="Федя, срочно пришли файл договора до 18:00")
    repository.save_message(session, message)
    llm = FakeLLM(
        summary="Для сделки нужен файл договора до 18:00.",
        action="Прислать файл договора.",
        confidence=0.84,
    )
    email = FakeEmail()

    assert handle_p0_candidate(session, message, llm, email) is True
    assert len(email.sent) == 1
    assert repository.get_message(session, "1", 1).p0_classification == "P0_STRICT"


def test_sms_code_now_sends_strict_email_with_raw_text(session) -> None:
    raw_text = "нужен код из SMS сейчас"
    message = msg(text=raw_text)
    repository.save_message(session, message)
    llm = FakeLLM(
        summary="Для подтверждения входа нужен код из SMS сейчас.",
        action="Прислать код из SMS.",
        deadline_text="сейчас",
        confidence=0.95,
    )
    email = FakeEmail()

    assert handle_p0_candidate(session, message, llm, email) is True
    assert raw_text in email.sent[0][1]
    assert repository.get_message(session, "1", 1).p0_classification == "P0_STRICT"


def test_trusted_critical_sender_can_replace_deadline_or_risk(session, settings) -> None:
    settings.p0_trusted_sender_ids = "42"
    message = msg(text="Пришли резервную копию базы")
    repository.save_message(session, message)
    llm = FakeLLM(
        summary="Для восстановления нужна резервная копия базы.",
        action="Прислать резервную копию базы.",
        confidence=0.95,
    )

    assert handle_p0_candidate(session, message, llm, FakeEmail(), settings=settings) is True
    assert llm.payloads[0]["message"]["trusted_sender"] is True
    assert repository.get_message(session, "1", 1).p0_classification == "P0_STRICT"


def test_trusted_sender_with_concrete_contract_action_emails(session, settings) -> None:
    settings.p0_trusted_sender_ids = "42"
    raw_text = "пришли договор сейчас"
    message = msg(text=raw_text)
    repository.save_message(session, message)
    llm = FakeLLM(
        summary="Для сделки нужен договор сейчас.",
        action="Прислать договор.",
        confidence=0.95,
    )
    email = FakeEmail()

    assert handle_p0_candidate(session, message, llm, email, settings=settings) is True
    assert raw_text in email.sent[0][1]
    assert llm.payloads[0]["message"]["trusted_sender"] is True
    assert repository.get_message(session, "1", 1).p0_classification == "P0_STRICT"


def test_trusted_sender_hello_still_does_not_email(session, settings) -> None:
    settings.p0_trusted_sender_ids = "42"
    message = msg(text="привет")
    repository.save_message(session, message)
    email = FakeEmail()

    assert handle_p0_candidate(
        session,
        message,
        FakeLLM(status=P0Status.p0_strict, confidence=0.99),
        email,
        settings=settings,
    ) is False
    assert email.sent == []
    assert repository.pending_alert_jobs(session) == []


def test_trusted_sender_answer_today_sends_email(session, settings) -> None:
    settings.p0_trusted_sender_ids = "42"
    message = msg(text="можешь сегодня ответить?")
    repository.save_message(session, message)
    email = FakeEmail()

    assert handle_p0_candidate(
        session,
        message,
        FakeLLM(status=P0Status.p0_candidate, confidence=0.4),
        email,
        settings=settings,
    ) is True
    assert len(email.sent) == 1
    assert repository.get_message(session, "1", 1).p0_classification == "P0_STRICT"


def test_legacy_review_alert_is_not_retried_and_can_be_cancelled(session, now) -> None:
    job = repository.create_alert_job(
        session,
        chat_id="legacy",
        message_id=1,
        alert_type="review_private",
        subject="legacy",
        text_body="private text",
        html_body="<p>private text</p>",
        now=now,
    )
    email = FakeEmail()

    assert repository.retry_pending_alerts(session, email, now=now) == 0
    assert email.sent == []
    assert repository.cancel_legacy_alerts(session) == 1
    assert job.status == "cancelled"


def test_legacy_p0_alert_without_confirmed_p0_is_not_retried(session, now) -> None:
    message = msg(chat_id="legacy", message_id=2, text="old failed classifier")
    repository.save_message(session, message)
    job = repository.create_alert_job(
        session,
        chat_id="legacy",
        message_id=2,
        alert_type="p0",
        subject="legacy",
        text_body="private text",
        html_body="<p>private text</p>",
        now=now,
    )

    assert repository.retry_pending_alerts(session, FakeEmail(), now=now) == 0
    assert repository.cancel_legacy_alerts(session) == 1
    assert job.status == "cancelled"


def test_legacy_p0_with_false_positive_status_but_no_llm_marker_is_cancelled(session, now) -> None:
    message = msg(chat_id="legacy-marker", message_id=3, text="old override")
    repository.save_message(session, message)
    repository.mark_p0_classified(
        session,
        "legacy-marker",
        3,
        "P0",
        now,
        confidence=0.99,
    )
    job = repository.create_alert_job(
        session,
        chat_id="legacy-marker",
        message_id=3,
        alert_type="p0",
        subject="legacy",
        text_body="private text",
        html_body="<p>private text</p>",
        now=now,
    )
    email = FakeEmail()

    assert repository.retry_pending_alerts(session, email, now=now) == 0
    assert email.sent == []
    assert repository.cancel_legacy_alerts(session) == 1
    assert job.status == "cancelled"


def test_new_policy_p0_with_marker_and_confidence_can_retry(session, now) -> None:
    message = msg(chat_id="new-policy", message_id=4, text="urgent request")
    repository.save_message(session, message)
    repository.mark_p0_llm_called(session, "new-policy", 4, now)
    repository.mark_p0_classified(
        session,
        "new-policy",
        4,
        P0Status.p0_strict.value,
        now,
        confidence=0.95,
    )
    repository.create_alert_job(
        session,
        chat_id="new-policy",
        message_id=4,
        alert_type="p0",
        subject="urgent",
        text_body="safe body",
        html_body="<p>safe body</p>",
        now=now,
    )
    email = FakeEmail()

    assert repository.retry_pending_alerts(session, email, now=now) == 1
    assert len(email.sent) == 1


def test_legacy_non_p0_alert_types_are_cancelled(session, now) -> None:
    for index, alert_type in enumerate(["review_private", "review_group", "fallback_group_p0"]):
        repository.create_alert_job(
            session,
            chat_id=f"legacy-type-{index}",
            message_id=index + 1,
            alert_type=alert_type,
            subject="legacy",
            text_body="private text",
            html_body="<p>private text</p>",
            now=now,
        )

    assert repository.cancel_legacy_alerts(session) == 3
    assert repository.pending_alert_jobs(session) == []


def test_media_burst_does_not_exhaust_llm_cap(session, settings) -> None:
    settings.p0_max_llm_calls_per_hour = 1
    for message_id in range(1, 5):
        media = msg(message_id=message_id, text=None, media_type=MediaType.photo)
        repository.save_message(session, media)
        handle_p0_candidate(session, media, FakeLLM(), FakeEmail(), settings=settings)
    text_message = msg(message_id=5, text="обычный текст")
    repository.save_message(session, text_message)
    llm = FakeLLM(status=P0Status.not_p0)

    handle_p0_candidate(session, text_message, llm, FakeEmail(), settings=settings)

    assert llm.calls == 1


def test_incoming_private_text_triggers_immediate_llm_p0_classification(session, settings) -> None:
    message = msg(text="привет")
    repository.save_message(session, message)
    llm = FakeLLM(status=P0Status.not_p0)

    handle_p0_candidate(session, message, llm, FakeEmail(), settings=settings)

    assert llm.calls == 1


def test_outgoing_message_does_not_trigger_llm(session, settings) -> None:
    message = msg(text="я отвечу", is_outgoing=True)
    repository.save_message(session, message)
    llm = FakeLLM()

    email = FakeEmail()
    handle_p0_candidate(session, message, llm, email, settings=settings)

    assert llm.calls == 0
    assert email.sent == []
    assert repository.get_message(session, "1", 1).p0_classification == "NOT_P0"
    assert repository.pending_alert_jobs(session) == []


def test_non_text_media_does_not_trigger_llm_or_immediate_email(session, settings) -> None:
    message = msg(text=None, media_type=MediaType.voice)
    repository.save_message(session, message)
    llm = FakeLLM()

    email = FakeEmail()
    handle_p0_candidate(session, message, llm, email, settings=settings)

    stored = repository.get_message(session, message.chat_id, message.message_id)
    assert llm.calls == 0
    assert stored.p0_review_candidate is False
    assert stored.p0_classification == "NOT_P0"
    assert email.sent == []


def test_private_media_with_urgent_caption_can_send_p0_email(session, settings) -> None:
    message = msg(
        text="посмотри срочно",
        media_type=MediaType.video,
    )
    repository.save_message(session, message)
    email = FakeEmail()

    assert handle_p0_candidate(
        session, message, FakeLLM(status=P0Status.p0), email, settings=settings
    )
    assert len(email.sent) == 1
    assert email.sent[0][0].startswith("[СРОЧНО]")


def test_private_media_with_nonurgent_caption_does_not_send_email(session, settings) -> None:
    message = msg(text="видео с прогулки", media_type=MediaType.video)
    repository.save_message(session, message)
    email = FakeEmail()

    assert handle_p0_candidate(
        session,
        message,
        FakeLLM(status=P0Status.not_p0),
        email,
        settings=settings,
    ) is False
    assert email.sent == []
    assert repository.get_message(session, "1", 1).p0_classification == "NOT_P0"
    assert repository.pending_alert_jobs(session) == []


def test_group_message_without_routing_does_not_trigger_immediate_llm(session, settings) -> None:
    message = msg(chat_id="g1", chat_type=ChatType.group, text="обычное обсуждение")
    repository.save_message(session, message)
    llm = FakeLLM()

    handle_p0_candidate(session, message, llm, FakeEmail(), settings=settings)

    assert llm.calls == 0


def test_group_mention_sends_immediate_email(session, settings) -> None:
    settings.p0_mention_usernames = "me"
    message = msg(chat_id="g1", chat_type=ChatType.group, text="@me срочно посмотри")
    repository.save_message(session, message)
    llm = FakeLLM(status=P0Status.not_p0)
    email = FakeEmail()

    assert handle_p0_candidate(session, message, llm, email, settings=settings) is True

    assert llm.calls == 1
    assert len(email.sent) == 1
    assert llm.payloads[0]["message"]["policy"]["direct_mention"] is True
    assert llm.payloads[0]["message"]["policy"]["deterministic_strict"] is True
    assert repository.get_message(session, "g1", 1).p0_classification == "P0_STRICT"


def test_group_username_prefix_is_not_a_direct_mention(session, settings) -> None:
    settings.p0_mention_usernames = "me"
    message = msg(
        chat_id="g1",
        chat_type=ChatType.group,
        text="@media обсуждаем логотип",
    )
    repository.save_message(session, message)
    llm = FakeLLM(status=P0Status.p0_strict, confidence=0.99)
    email = FakeEmail()

    assert handle_p0_candidate(session, message, llm, email, settings=settings) is False
    assert llm.calls == 0
    assert email.sent == []
    assert repository.pending_alert_jobs(session) == []


def test_group_reply_with_request_sends_immediate_email(session, settings) -> None:
    parent = msg(chat_id="g1", chat_type=ChatType.group, message_id=1, is_outgoing=True)
    message = msg(
        chat_id="g1",
        chat_type=ChatType.group,
        message_id=2,
        text="можешь сегодня ответить?",
        reply_to_message_id=1,
    )
    repository.save_message(session, parent)
    repository.save_message(session, message)
    llm = FakeLLM(status=P0Status.not_p0)
    email = FakeEmail()

    assert handle_p0_candidate(session, message, llm, email, settings=settings) is True

    assert llm.calls == 1
    assert len(email.sent) == 1
    assert repository.get_message(session, "g1", 2).p0_classification == "P0_STRICT"


def test_group_reply_with_missing_parent_stays_fail_closed(session, settings) -> None:
    message = msg(
        chat_id="g1",
        chat_type=ChatType.group,
        text="можешь сегодня ответить?",
        reply_to_message_id=999,
    )
    repository.save_message(session, message)
    llm = FakeLLM(status=P0Status.p0_strict, confidence=0.99)
    email = FakeEmail()

    assert handle_p0_candidate(session, message, llm, email, settings=settings) is False
    assert llm.calls == 0
    assert email.sent == []
    assert repository.pending_alert_jobs(session) == []


def test_unrouted_group_urgency_does_not_send_email(session, settings) -> None:
    message = msg(
        chat_id="g1",
        chat_type=ChatType.group,
        text="urgent reply please now about this",
    )
    repository.save_message(session, message)
    llm = FakeLLM(status=P0Status.p0_strict, confidence=0.99)
    email = FakeEmail()

    assert handle_p0_candidate(session, message, llm, email, settings=settings) is False
    assert llm.calls == 0
    assert email.sent == []
    assert repository.pending_alert_jobs(session) == []


def test_group_deadline_plus_urgency_sends_email(session, settings) -> None:
    message = msg(
        chat_id="g1",
        chat_type=ChatType.group,
        text="нужно решение до 18:00, срочно",
    )
    repository.save_message(session, message)
    email = FakeEmail()

    assert handle_p0_candidate(
        session,
        message,
        FakeLLM(status=P0Status.not_p0),
        email,
        settings=settings,
    ) is True
    assert len(email.sent) == 1
    assert repository.get_message(session, "g1", 1).p0_classification == "P0_STRICT"


def test_group_watchlist_keyword_with_request_sends_email(session, settings) -> None:
    settings.p0_watchlist_keywords = "production"
    message = msg(
        chat_id="g1",
        chat_type=ChatType.group,
        text="production: срочно проверь",
    )
    repository.save_message(session, message)
    email = FakeEmail()

    assert handle_p0_candidate(
        session,
        message,
        FakeLLM(status=P0Status.not_p0),
        email,
        settings=settings,
    ) is True
    assert len(email.sent) == 1


def test_group_watchlist_without_request_does_not_trigger_llm(session, settings) -> None:
    settings.p0_watchlist_chat_ids = "g1"
    message = msg(chat_id="g1", chat_type=ChatType.group, text="обычное обсуждение")
    repository.save_message(session, message)
    llm = FakeLLM(status=P0Status.not_p0)

    handle_p0_candidate(session, message, llm, FakeEmail(), settings=settings)

    assert llm.calls == 0


def test_only_p0_sends_immediate_email(session, settings) -> None:
    p0_message = msg(message_id=11, text="Пришли файл договора сегодня до 18:00")
    review_message = msg(message_id=12, text="привет")
    repository.save_message(session, p0_message)
    repository.save_message(session, review_message)
    email = FakeEmail()

    handle_p0_candidate(session, p0_message, FakeLLM(status=P0Status.p0), email, settings=settings)
    handle_p0_candidate(
        session,
        review_message,
        FakeLLM(status=P0Status.review),
        email,
        settings=settings,
    )

    assert len(email.sent) == 1


def test_private_hello_does_not_email_even_for_strict_llm(session, settings) -> None:
    message = msg(text="привет")
    repository.save_message(session, message)
    email = FakeEmail()

    handle_p0_candidate(
        session,
        message,
        FakeLLM(status=P0Status.p0_strict, confidence=0.99),
        email,
        settings=settings,
    )

    assert email.sent == []


def test_same_message_is_not_classified_twice(session, settings) -> None:
    message = msg(text="привет")
    repository.save_message(session, message)
    llm = FakeLLM(status=P0Status.not_p0)

    handle_p0_candidate(session, message, llm, FakeEmail(), settings=settings)
    handle_p0_candidate(session, message, llm, FakeEmail(), settings=settings)

    assert llm.calls == 1


def test_hourly_llm_cap_hit_private_stays_in_digest(session, settings) -> None:
    settings.p0_max_llm_calls_per_hour = 0
    message = msg(text="привет")
    repository.save_message(session, message)
    email = FakeEmail()
    llm = FakeLLM(status=P0Status.not_p0)

    handle_p0_candidate(session, message, llm, email, settings=settings)

    assert llm.calls == 0
    assert email.sent == []
    assert repository.get_message(session, "1", 1).p0_review_candidate is True


def test_deterministic_private_p0_works_at_llm_cap_and_retries(session, settings, now) -> None:
    settings.p0_max_llm_calls_per_hour = 0
    message = msg(text="ответь срочно сейчас об этом", timestamp=now)
    repository.save_message(session, message)
    llm = FakeLLM(status=P0Status.not_p0)

    assert handle_p0_candidate(
        session,
        message,
        llm,
        FakeEmail(fail=True),
        settings=settings,
    ) is True
    stored = repository.get_message(session, "1", 1)
    assert llm.calls == 0
    assert stored.p0_llm_called_at is None
    assert stored.p0_classification == "P0_STRICT"

    job = repository.pending_alert_jobs(session)[0]
    assert repository.retry_pending_alerts(session, FakeEmail(), now=job.next_attempt_at) == 1


def test_p0_classifier_message_text_is_capped(session, settings) -> None:
    settings.p0_max_message_chars = 20
    message = msg(text="x" * 100)
    repository.save_message(session, message)
    llm = FakeLLM(status=P0Status.not_p0)

    handle_p0_candidate(session, message, llm, FakeEmail(), settings=settings)

    assert len(llm.payloads[0]["message"]["text"]) <= 20
    assert llm.payloads[0]["message"]["trusted_sender"] is False
