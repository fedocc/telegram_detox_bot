from __future__ import annotations

from app.db import repository
from app.email.sender import EmailSendError
from app.llm.client import LLMError
from app.models.schemas import ChatType, MediaType, P0Decision, P0Status
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


class FailingGmailApiEmail(FakeEmail):
    def send(self, subject: str, text: str, html: str | None = None, **kwargs) -> None:
        raise EmailSendError("Gmail API send failed")


class FakeLLM:
    def __init__(
        self,
        fail: bool = False,
        status: P0Status = P0Status.p0,
        deadline_text: str | None = None,
    ) -> None:
        self.fail = fail
        self.status = status
        self.deadline_text = deadline_text
        self.calls = 0
        self.payloads: list[dict] = []

    def classify_p0(self, payload: dict) -> P0Decision:
        self.calls += 1
        self.payloads.append(payload)
        if self.fail:
            raise LLMError("down")
        return P0Decision(
            status=self.status,
            summary="Просит позвонить через час.",
            action="Позвонить.",
            deadline_text=self.deadline_text,
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


def test_pending_p0_retry_works_when_gmail_api_send_fails(session) -> None:
    message = msg(text="Позвони через час")
    repository.save_message(session, message)

    assert handle_p0_candidate(session, message, FakeLLM(fail=True), FailingGmailApiEmail())

    jobs = repository.pending_alert_jobs(session)
    assert len(jobs) == 1
    assert jobs[0].last_error_safe == "EmailSendError"


def test_pending_p0_alert_is_retried_and_marked_sent(session, now) -> None:
    message = msg(text="Позвони через час", timestamp=now)
    repository.save_message(session, message)
    handle_p0_candidate(session, message, FakeLLM(fail=True), FakeEmail(fail=True))

    job = repository.pending_alert_jobs(session)[0]
    sent = repository.retry_pending_alerts(session, FakeEmail(), now=job.next_attempt_at)

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


def test_first_failed_alert_waits_one_minute_before_retry(session, now) -> None:
    message = msg(text="Позвони через час", timestamp=now)
    repository.save_message(session, message)

    handle_p0_candidate(session, message, FakeLLM(fail=True), FakeEmail(fail=True))
    job = repository.pending_alert_jobs(session)[0]

    assert job.attempts == 1
    assert job.next_attempt_at > now.replace(tzinfo=None)
    assert repository.retry_pending_alerts(session, FakeEmail(), now=now) == 0


def test_retry_respects_next_attempt_at(session, now) -> None:
    message = msg(text="Позвони через час", timestamp=now)
    repository.save_message(session, message)
    handle_p0_candidate(session, message, FakeLLM(fail=True), FakeEmail(fail=True))
    email = FakeEmail()

    assert repository.retry_pending_alerts(session, email, now=now) == 0
    assert email.sent == []


def test_backoff_caps_at_sixty_minutes(session, now) -> None:
    from datetime import timedelta

    message = msg(text="Позвони через час", timestamp=now)
    repository.save_message(session, message)
    handle_p0_candidate(session, message, FakeLLM(fail=True), FakeEmail(fail=True))
    job = repository.pending_alert_jobs(session)[0]
    for _ in range(5):
        repository.retry_pending_alerts(session, FakeEmail(fail=True), now=job.next_attempt_at)
        job = repository.pending_alert_jobs(session)[0]

    assert job.next_attempt_at <= now.replace(tzinfo=None) + timedelta(
        minutes=1 + 5 + 15 + 60 + 60 + 60
    )


def test_two_workers_cannot_claim_same_alert_job(session, now) -> None:
    message = msg(text="Позвони через час", timestamp=now)
    repository.save_message(session, message)
    handle_p0_candidate(session, message, FakeLLM(fail=True), FakeEmail(fail=True))
    job = repository.pending_alert_jobs(session)[0]

    first = repository.claim_pending_alert(session, job.id, job.next_attempt_at, "token-1")
    second = repository.claim_pending_alert(session, job.id, job.next_attempt_at, "token-2")

    assert first is not None
    assert second is None


def test_stale_sending_alert_becomes_retryable(session, now) -> None:
    message = msg(text="Позвони через час", timestamp=now)
    repository.save_message(session, message)
    handle_p0_candidate(session, message, FakeLLM(fail=True), FakeEmail(fail=True))
    job = repository.pending_alert_jobs(session)[0]
    repository.claim_pending_alert(session, job.id, job.next_attempt_at, "token-1")

    repository.release_stale_alert_claims(session, job.next_attempt_at, stale_minutes=0)

    assert repository.pending_alert_jobs(session)


def test_claimed_job_is_not_sent_by_second_worker(session, now) -> None:
    message = msg(text="Позвони через час", timestamp=now)
    repository.save_message(session, message)
    handle_p0_candidate(session, message, FakeLLM(fail=True), FakeEmail(fail=True))
    job = repository.pending_alert_jobs(session)[0]
    repository.claim_pending_alert(session, job.id, job.next_attempt_at, "token-1")
    email = FakeEmail()

    assert (
        repository.send_claimed_alert(session, job.id, "token-2", email, job.next_attempt_at)
        is False
    )
    assert email.sent == []


def test_p0_malformed_provider_response_sends_fallback_email(session, settings) -> None:
    from app.llm.client import HaikuClient
    from tests.test_llm_errors import FakeClient, MalformedCompletions

    message = msg(text="можешь посмотреть?")
    repository.save_message(session, message)
    client = HaikuClient(settings)
    client.client = FakeClient(MalformedCompletions(type("Response", (), {"choices": []})()))
    email = FakeEmail()

    assert handle_p0_candidate(session, message, client, email) is True
    assert email.sent[0][0] == "[ПРОВЕРЬ] новое личное сообщение"


def test_p0_real_openai_error_uses_fallback_email(session, settings) -> None:
    from app.llm.client import HaikuClient
    from tests.test_llm_errors import BrokenClient

    message = msg(text="можешь посмотреть?")
    repository.save_message(session, message)
    client = HaikuClient(settings)
    client.client = BrokenClient()
    email = FakeEmail()

    assert handle_p0_candidate(session, message, client, email) is True
    assert email.sent


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


def test_call_back_in_one_hour_is_p0() -> None:
    from app.services.prefilter import is_urgent_call_candidate

    assert is_urgent_call_candidate("Please call me back in one hour")


def test_pozvoni_cherez_chas_is_p0() -> None:
    from app.services.prefilter import is_urgent_call_candidate

    assert is_urgent_call_candidate("Позвони через час")


def test_join_call_in_thirty_minutes_is_p0() -> None:
    from app.services.prefilter import is_urgent_call_candidate

    assert is_urgent_call_candidate("Please join the call in 30 minutes")


def test_call_tomorrow_without_urgency_is_not_forced_p0(session) -> None:
    message = msg(text="Can we call tomorrow?")
    repository.save_message(session, message)
    email = FakeEmail()

    assert handle_p0_candidate(session, message, FakeLLM(status=P0Status.not_p0), email) is False
    assert email.sent == []
    assert repository.pending_alert_jobs(session) == []


def test_llm_not_p0_is_overridden_for_urgent_call_candidate(session) -> None:
    message = msg(text="Please call me back in one hour")
    repository.save_message(session, message)
    email = FakeEmail()

    assert handle_p0_candidate(session, message, FakeLLM(status=P0Status.not_p0), email) is True

    assert email.sent[0][0].startswith("[СРОЧНО]")
    assert "deterministic_urgent_call_override" in email.sent[0][1]


def test_override_keeps_deadline_text(session) -> None:
    message = msg(text="Please call me back in one hour")
    repository.save_message(session, message)
    email = FakeEmail()

    handle_p0_candidate(
        session,
        message,
        FakeLLM(status=P0Status.not_p0, deadline_text="in 1 hour"),
        email,
    )

    assert "in 1 hour" in email.sent[0][1]


def test_override_creates_immediate_alert_job(session) -> None:
    message = msg(text="Перезвони мне через 30 минут")
    repository.save_message(session, message)
    email = FakeEmail()

    handle_p0_candidate(session, message, FakeLLM(status=P0Status.not_p0), email)

    jobs = repository.pending_alert_jobs(session)
    assert len(email.sent) == 1
    assert jobs == []
    stored = repository.get_message(session, message.chat_id, message.message_id)
    assert stored.alert_sent is True


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

    handle_p0_candidate(session, message, llm, FakeEmail(), settings=settings)

    assert llm.calls == 0


def test_non_text_media_does_not_trigger_llm_and_goes_to_manual_review(session, settings) -> None:
    message = msg(text=None, media_type=MediaType.voice)
    repository.save_message(session, message)
    llm = FakeLLM()

    email = FakeEmail()
    handle_p0_candidate(session, message, llm, email, settings=settings)

    stored = repository.get_message(session, message.chat_id, message.message_id)
    assert llm.calls == 0
    assert stored.p0_review_candidate is True
    assert len(email.sent) == 1
    assert email.sent[0][0] == "[ПРОВЕРЬ] возможно важное личное сообщение"


def test_group_message_without_routing_does_not_trigger_immediate_llm(session, settings) -> None:
    message = msg(chat_id="g1", chat_type=ChatType.group, text="обычное обсуждение")
    repository.save_message(session, message)
    llm = FakeLLM()

    handle_p0_candidate(session, message, llm, FakeEmail(), settings=settings)

    assert llm.calls == 0


def test_group_mention_triggers_immediate_llm(session, settings) -> None:
    message = msg(chat_id="g1", chat_type=ChatType.group, text="@me проверь пожалуйста")
    repository.save_message(session, message)
    llm = FakeLLM(status=P0Status.not_p0)

    handle_p0_candidate(session, message, llm, FakeEmail(), settings=settings)

    assert llm.calls == 1


def test_group_reply_triggers_immediate_llm(session, settings) -> None:
    parent = msg(chat_id="g1", chat_type=ChatType.group, message_id=1, is_outgoing=True)
    message = msg(
        chat_id="g1",
        chat_type=ChatType.group,
        message_id=2,
        text="ответ",
        reply_to_message_id=1,
    )
    repository.save_message(session, parent)
    repository.save_message(session, message)
    llm = FakeLLM(status=P0Status.not_p0)

    handle_p0_candidate(session, message, llm, FakeEmail(), settings=settings)

    assert llm.calls == 1


def test_group_watchlist_triggers_immediate_llm(session, settings) -> None:
    settings.p0_watchlist_chat_ids = "g1"
    message = msg(chat_id="g1", chat_type=ChatType.group, text="обычное обсуждение")
    repository.save_message(session, message)
    llm = FakeLLM(status=P0Status.not_p0)

    handle_p0_candidate(session, message, llm, FakeEmail(), settings=settings)

    assert llm.calls == 1


def test_p0_and_review_both_send_immediate_email(session, settings) -> None:
    p0_message = msg(message_id=11, text="проверь")
    review_message = msg(message_id=12, text="проверь")
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

    assert len(email.sent) == 2


def test_not_p0_does_not_send_immediate_email(session, settings) -> None:
    message = msg(text="привет")
    repository.save_message(session, message)
    email = FakeEmail()

    handle_p0_candidate(session, message, FakeLLM(status=P0Status.not_p0), email, settings=settings)

    assert email.sent == []


def test_same_message_is_not_classified_twice(session, settings) -> None:
    message = msg(text="привет")
    repository.save_message(session, message)
    llm = FakeLLM(status=P0Status.not_p0)

    handle_p0_candidate(session, message, llm, FakeEmail(), settings=settings)
    handle_p0_candidate(session, message, llm, FakeEmail(), settings=settings)

    assert llm.calls == 1


def test_hourly_llm_cap_hit_private_fails_open_review_email(session, settings) -> None:
    settings.p0_max_llm_calls_per_hour = 0
    message = msg(text="привет")
    repository.save_message(session, message)
    email = FakeEmail()
    llm = FakeLLM(status=P0Status.not_p0)

    handle_p0_candidate(session, message, llm, email, settings=settings)

    assert llm.calls == 0
    assert len(email.sent) == 1
    assert "budget cap hit" in email.sent[0][1]


def test_p0_classifier_message_text_is_capped(session, settings) -> None:
    settings.p0_max_message_chars = 20
    message = msg(text="x" * 100)
    repository.save_message(session, message)
    llm = FakeLLM(status=P0Status.not_p0)

    handle_p0_candidate(session, message, llm, FakeEmail(), settings=settings)

    assert len(llm.payloads[0]["message"]["text"]) <= 20
