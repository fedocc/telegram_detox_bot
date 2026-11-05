from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp.test_utils import TestClient, TestServer
from telethon import functions, types
from telethon.errors import FloodWaitError, RPCError

from app.inbox.content import poll_content, poll_option_id
from app.inbox.errors import InboxError
from app.inbox.store import LIFETIME
from app.inbox.web import create_app
from tests.test_inbox import Message, activate
from tests.test_inbox import service as service
from tests.test_inbox_content import poll_message
from tests.test_inbox_interactions import FeatureTelegram, headers, seed_digest

POLL_ID = "9223372036854775800"
A, B = poll_option_id(b"a"), poll_option_id(b"b")


class VotingTelegram(FeatureTelegram):
    def __init__(self):
        super().__init__()
        self.messages = [poll_message()]
        for item in self.messages[0].media.results.results:
            item.chosen = False
        self.vote_error = None
        self.min_result = False

    async def __call__(self, request):
        if isinstance(request, functions.messages.GetPollResultsRequest):
            self.requests.append(request)
            media = next(item.media for item in self.messages if item.id == request.msg_id)
            results = deepcopy(media.results)
            results.min = False
            return SimpleNamespace(updates=[types.UpdateMessagePoll(media.poll.id, results)])
        if not isinstance(request, functions.messages.SendVoteRequest):
            return await super().__call__(request)
        self.requests.append(request)
        if self.vote_error:
            raise self.vote_error
        media = next(item.media for item in self.messages if item.id == request.msg_id)
        old = {item.option for item in media.results.results if item.chosen}
        selected = set(request.options)
        media.results.total_voters += bool(selected) - bool(old)
        for result in media.results.results:
            result.voters += (result.option in selected) - (result.option in old)
            result.chosen = result.option in selected
        results = deepcopy(media.results)
        if self.min_result:
            results.min = True
            for item in results.results:
                item.chosen = None
        return SimpleNamespace(updates=[types.UpdateMessagePoll(media.poll.id, results)])


@pytest.fixture()
def voting_service(service):
    service.client = VotingTelegram()
    return service


async def test_single_vote_change_retract_and_retry_keep_access(voting_service):
    service = voting_service
    row = activate(service, peer="42", opened=False)
    for options, chosen in [([A], [True, False]), ([A], [True, False]),
                            ([B], [False, True]), ([], [False, False])]:
        result = await service.vote_poll("conversations", row.id, 100, POLL_ID, options)
        assert [item["chosen"] for item in result["content"]["options"]] == chosen
    assert [r.options for r in service.client.requests] == [[b"a"], [b"b"], []]
    assert service.client.requests[0].msg_id == 100
    after = service.store.get_any(row.id)
    assert after.opened_at == row.opened_at and after.expires_at == row.expires_at
    assert service.manual_open_status()["remaining"] == 2
    assert service.store.notifications(after=0)["events"] == []


async def test_multiple_choice_vote_uses_all_selected_options_and_min_results_keep_own_vote(
    voting_service,
):
    service = voting_service
    service.client.messages[0].media.poll.multiple_choice = True
    service.client.min_result = True
    row = activate(service, peer="42")
    result = await service.vote_poll("conversations", row.id, 100, POLL_ID, [B, A])
    assert service.client.requests[0].options == [b"b", b"a"]
    assert all(item["chosen"] for item in result["content"]["options"])
    assert [item["voters"] for item in result["content"]["options"]] == [3, 4]


@pytest.mark.parametrize("options", ["a", [A, A], [True], [None], ["x" * 257],
                                      [A] * 101, ["unknown"], [A, B]])
async def test_invalid_or_multiple_options_in_single_poll_never_send(voting_service, options):
    service = voting_service
    row = activate(service, peer="42")
    with pytest.raises(InboxError):
        await service.vote_poll("conversations", row.id, 100, POLL_ID, options)
    assert service.client.requests == []


@pytest.mark.parametrize("flag", ["closed", "quiz", "revoting_disabled"])
async def test_closed_poll_or_locked_answer_never_changes(voting_service, flag):
    service = voting_service
    media = service.client.messages[0].media
    setattr(media.poll, flag, True)
    media.results.results[1].chosen = True
    row = activate(service, peer="42")
    with pytest.raises(InboxError):
        await service.vote_poll("conversations", row.id, 100, POLL_ID, [B])
    assert service.client.requests == []


@pytest.mark.parametrize("case", ["closed_chat", "expired", "foreign_topic", "changed_poll",
                                   "not_poll", "offline", "ignored", "digest"])
async def test_unauthorized_or_unavailable_target_never_sends(voting_service, case):
    service = voting_service
    row = activate(service, peer="42", thread=55 if case == "foreign_topic" else 0,
                   forum=case == "foreign_topic")
    scope, poll_id = "conversations", POLL_ID
    if case == "closed_chat":
        service.store.close(row.id)
    elif case == "expired":
        service.test_clock[0] += LIFETIME + 1
    elif case == "changed_poll":
        poll_id = "123"
    elif case == "not_poll":
        service.client.messages = [Message(100)]
    elif case == "offline":
        service.client.connected = False
    elif case == "ignored":
        service.test_ignored.add("42")
    elif case == "digest":
        seed_digest(service)
        scope = "digest"
    with pytest.raises(InboxError):
        await service.vote_poll(scope, row.id, 100, poll_id, [A])
    assert service.client.requests == []


async def test_expiry_during_peer_resolution_is_rechecked_before_vote(voting_service):
    service = voting_service
    row = activate(service, peer="42")
    original = service._resolve_peer
    calls = 0

    async def resolve(target, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            service.test_clock[0] += LIFETIME + 1
        return await original(target, **kwargs)

    service._resolve_peer = resolve
    with pytest.raises(InboxError):
        await service.vote_poll("conversations", row.id, 100, POLL_ID, [A])
    assert service.client.requests == []


async def test_library_broadcast_poll_can_be_voted_without_message_send_rights(voting_service):
    service = voting_service
    source = service.store.upsert_library_source(source_id="channel", peer_type="channel",
        peer_id="-100123", access_hash=123, display_title="Учебный канал", is_bot=False)
    service._verify_library_write = AsyncMock(side_effect=AssertionError("Send rights not needed"))
    result = await service.vote_poll("library", source.id, 100, POLL_ID, [A])
    assert result["content"]["voted"]
    assert not service.store.active()
    service.store.update_library_source(source.id, library_enabled=False)
    with pytest.raises(InboxError):
        await service.vote_poll("library", source.id, 100, POLL_ID, [B])
    assert len(service.client.requests) == 1


@pytest.mark.parametrize("code", ["MESSAGE_POLL_CLOSED", "REVOTE_NOT_ALLOWED",
                                   "POLL_MEMBER_RESTRICTED", "POLL_COUNTRY_RESTRICTED"])
async def test_telegram_rejection_is_readable_and_never_claims_success(voting_service, code):
    service = voting_service
    row = activate(service, peer="42")
    service.client.vote_error = RPCError(None, code, 400)
    with pytest.raises(InboxError) as error:
        await service.vote_poll("conversations", row.id, 100, POLL_ID, [A])
    assert error.value.status == 422
    assert code not in str(error.value)


async def test_vote_flood_wait_uses_shared_cooldown(voting_service):
    service = voting_service
    row = activate(service, peer="42")
    service.client.vote_error = FloodWaitError(None, 20)
    with pytest.raises(InboxError) as error:
        await service.vote_poll("conversations", row.id, 100, POLL_ID, [A])
    assert error.value.status == 429
    with pytest.raises(InboxError):
        await service.vote_poll("conversations", row.id, 100, POLL_ID, [A])
    assert len(service.client.requests) == 1


async def test_vote_endpoint_requires_csrf_and_rejects_forged_shape(voting_service):
    service = voting_service
    row = activate(service, peer="42")
    path = f"/api/conversations/{row.id}/messages/100/vote"
    body = {"poll_id": POLL_ID, "options": [A]}
    async with TestClient(TestServer(create_app(service)),
                          headers={"Host": "127.0.0.1:8787"}) as client:
        assert (await client.post(path, json=body)).status == 403
        auth = await headers(client)
        assert (await client.post(path, json={**body, "peer_id": "43"}, headers=auth)).status == 400
        response = await client.post(path, json=body, headers=auth)
        assert response.status == 200
        assert (await response.json())["content"]["voted"]
        assert (await client.post("/api/digest/dtest/messages/100/vote",
                                  json=body, headers=auth)).status == 404
    assert len(service.client.requests) == 1


def test_poll_updates_invalidate_only_matching_cached_views_without_access(voting_service):
    service = voting_service
    message = service.client.messages[0]
    service.snapshots.update({"matching": {"messages": {100: message}},
                              "other": {"messages": {101: Message(101)}}})
    service.library_snapshots["library"] = {"messages": {100: message}}
    service.pin_snapshots["pins"] = {
        "payload": {"pins": [{"content": poll_content(message.media)}]}}
    service.invalidate_poll(POLL_ID)
    assert list(service.snapshots) == ["other"]
    assert not service.library_snapshots and not service.pin_snapshots
    assert not service.store.active()


async def test_min_history_results_are_completed_once_and_cache_does_not_reopen_closed_poll(
    voting_service,
):
    service = voting_service
    row = activate(service, peer="42")
    message = service.client.messages[0]
    message.media.results.min = True
    first = await service.serialize(message, row, {})
    assert first["content"]["title"] == "Когда встречаемся?"
    assert len(service.client.requests) == 1
    assert isinstance(service.client.requests[0], functions.messages.GetPollResultsRequest)
    message.media.results.min = True
    message.media.poll.closed = True
    second = await service.serialize(message, row, {})
    assert second["content"]["closed"]
    assert len(service.client.requests) == 1


async def test_min_results_protect_already_answered_quiz(voting_service):
    service = voting_service
    row = activate(service, peer="42")
    media = service.client.messages[0].media
    media.poll.quiz = True
    media.results.min = True
    media.results.results[1].chosen = True
    with pytest.raises(InboxError):
        await service.vote_poll("conversations", row.id, 100, POLL_ID, [B])
    assert len(service.client.requests) == 1
    assert isinstance(service.client.requests[0], functions.messages.GetPollResultsRequest)


async def test_vote_and_live_poll_updates_preserve_loaded_history(voting_service):
    service = voting_service
    row = activate(service, peer="42")
    service.client.messages.extend(Message(mid) for mid in range(101, 151))
    await service.history(row.id)
    loaded = set(service.snapshots[row.id]["messages"])
    await service.vote_poll("conversations", row.id, 100, POLL_ID, [A])
    assert set(service.snapshots[row.id]["messages"]) == loaded
    after_vote = await service.history(row.id)
    assert after_vote["messages"][0]["content"]["options"][0]["chosen"]
    results = deepcopy(service.client.messages[0].media.results)
    next(item for item in results.results if item.option == b"b").voters = 4
    service.invalidate_poll(POLL_ID, update=types.UpdateMessagePoll(int(POLL_ID), results))
    assert set(service.snapshots[row.id]["messages"]) == loaded
    after_update = await service.history(row.id)
    assert after_update["messages"][0]["content"]["options"][1]["voters"] == 4
