import json
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import TestClient, TestServer
from telethon.tl import types

from app.inbox.content import message_content, poll_content
from app.inbox.service import reply_preview_text
from app.inbox.web import create_app
from tests.test_inbox import Message, activate
from tests.test_inbox import service as service
from tests.test_inbox_interactions import seed_digest


def poll_message(mid=100, *, rich=True, **flags):
    def text(value):
        return types.TextWithEntities(value, []) if rich else value

    poll = SimpleNamespace(question=text("Когда встречаемся?"), answers=[
        SimpleNamespace(text=text("Во вторник"), option=b"a"),
        SimpleNamespace(text=text("В среду"), option=b"b"),
    ], **flags)
    results = types.PollResults(total_voters=5, results=[
        types.PollAnswerVoters(b"b", voters=3),
        types.PollAnswerVoters(b"a", voters=2, chosen=True),
    ])
    return Message(mid, "", media=types.MessageMediaPoll(poll, results))


@pytest.mark.parametrize("rich", [True, False])
async def test_poll_without_file_is_rendered_with_option_matched_results(service, rich):
    row = activate(service)
    message = poll_message(rich=rich)
    result = await service.serialize(message, row, {})
    assert result["media"] is None and result["text"] == ""
    content = result["content"]
    assert content["title"] == "Когда встречаемся?"
    assert [item["percent"] for item in content["options"]] == [40, 60]
    assert [item["voters"] for item in content["options"]] == [2, 3]
    assert content["options"][0]["chosen"]
    assert reply_preview_text(message) == "Опрос: Когда встречаемся?"
    assert service.client.downloads == 0


def test_unanswered_poll_does_not_reveal_hidden_results_or_quiz_solution():
    media = poll_message(quiz=True).media
    media.results.results[1].chosen = False
    media.results.results[0].correct = True
    media.results.solution = "Объяснение ответа"
    media.results.recent_voters = [types.PeerUser(987654321)]
    content = poll_content(media)
    assert content["total_voters"] == 5
    assert all(item["voters"] is None and item["percent"] is None
               and not item["correct"] for item in content["options"])
    assert content["solution"] == ""
    assert "987654321" not in json.dumps(content)
    media.poll.closed = True
    closed = poll_content(media)
    assert closed["options"][1]["correct"]
    assert closed["solution"] == "Объяснение ответа"


def test_hidden_until_close_and_multiple_choice_counts_use_voters_as_denominator():
    media = poll_message(multiple_choice=True, hide_results_until_close=True).media
    assert poll_content(media)["options"][0]["percent"] is None
    media.poll.closed = True
    media.results.results[0].voters = 5
    media.results.results[1].voters = 5
    assert [item["percent"] for item in poll_content(media)["options"]] == [100, 100]
    media.results.total_voters = None
    assert all(item["percent"] is None for item in poll_content(media)["options"])


@pytest.mark.parametrize("scope", ["inbox", "library"])
async def test_poll_reply_opens_outside_history_and_pins_keep_poll(service, scope):
    row = activate(service, trigger=200)
    parent = poll_message(7)
    answer = Message(200, "Ответ", reply_to_msg_id=7, parent=parent)
    parent.pinned = True
    service.client.messages = [parent, *[Message(mid) for mid in range(100, 200)], answer]
    if scope == "inbox":
        path = f"/api/conversations/{row.id}"
    else:
        source = service.store.upsert_library_source(source_id="a" * 32,
            peer_type="channel", peer_id="-100123", display_title="Тестовая группа",
            library_enabled=True, access_hash=123, is_bot=False)
        path = f"/api/library/{source.id}"
    before = service.store.get_any(row.id)
    async with TestClient(TestServer(create_app(service)),
                          headers={"Host": "127.0.0.1:8787"}) as client:
        history = await (await client.get(f"{path}/messages")).json()
        assert 7 not in [item["id"] for item in history["messages"]]
        assert history["messages"][-1]["reply"]["text"] == "Опрос: Когда встречаемся?"
        response = await client.get(f"{path}/messages/7")
        assert response.status == 200
        assert (await response.json())["message"]["content"]["kind"] == "poll"
        pins = await (await client.get(f"{path}/pins")).json()
        assert pins["pins"][0]["content"]["kind"] == "poll"
    after = service.store.get_any(row.id)
    assert (before.opened_at, before.expires_at) == (after.opened_at, after.expires_at)
    service.client.send_message.assert_not_called()
    service.client.send_file.assert_not_called()


async def test_poll_from_digest_is_exact_and_never_activates_chat(service):
    seed_digest(service)
    service.client.messages = [poll_message()]
    preview = await service.digest_preview("dtest", 100)
    assert preview["message"]["content"]["kind"] == "poll"
    assert not service.store.active()
    assert service.manual_open_status()["remaining"] == 2


@pytest.mark.parametrize("media,kind,title", [
    (types.MessageMediaContact("+79990000000", "Тест", "Контакт", "private-vcard", 42),
     "contact", "Тест Контакт"),
    (types.MessageMediaGeo(types.GeoPoint(37.62, 55.75, 987654321)),
     "location", "Геопозиция"),
    (types.MessageMediaVenue(types.GeoPoint(37.62, 55.75, 987654321),
                            "Аудитория", "Учебный корпус", "", "", ""),
     "location", "Аудитория"),
    (types.MessageMediaDice(4, "🎲"), "dice", "🎲"),
    (types.MessageMediaUnsupported(), "unsupported", "Вложение"),
    (SimpleNamespace(), "unsupported", "Вложение"),
    (types.MessageMediaPhoto(types.PhotoEmpty(42)), "unsupported", "Фото недоступно"),
])
async def test_non_file_media_no_longer_disappears(service, media, kind, title):
    message = Message(100, "", media=media)
    serialized = await service.serialize(message, activate(service), {})
    assert serialized["content"]["kind"] == kind
    assert serialized["content"]["title"] == title
    encoded = json.dumps(serialized)
    assert "private-vcard" not in encoded and "987654321" not in encoded
    assert service.client.downloads == 0


def test_web_preview_missing_caption_is_visible_but_pending_preview_adds_no_noise():
    page = SimpleNamespace(title="Учебная заметка", description="Подробности",
                           url="https://example.org", site_name="Материалы")
    message = Message(100, "", media=types.MessageMediaWebPage(page))
    assert message_content(message)["title"] == "Учебная заметка"
    message.media.webpage = types.WebPageEmpty(42)
    message.raw_text = "https://example.org"
    assert message_content(message) is None


@pytest.mark.parametrize("lat,lon", [(float("nan"), 0), (0, float("inf")), (91, 0), (0, 181)])
def test_invalid_geo_coordinates_are_not_exposed(lat, lon):
    content = message_content(Message(100, "", media=types.MessageMediaGeo(
        SimpleNamespace(lat=lat, long=lon))))
    assert "latitude" not in content and "longitude" not in content


async def test_truly_empty_or_deleted_messages_still_do_not_make_blank_bubbles(service):
    row = activate(service)
    assert await service.serialize(Message(101, "", media=types.MessageMediaEmpty()), row, {}) \
        is None
    assert await service.serialize(types.MessageEmpty(102, types.PeerUser(42)), row, {}) is None


async def test_content_module_is_served_for_browser_and_offline_shell(service):
    async with TestClient(TestServer(create_app(service)),
                          headers={"Host": "127.0.0.1:8787"}) as client:
        response = await client.get("/static/content.mjs?v=29")
        assert response.status == 200
        assert "javascript" in response.content_type
        assert "export function renderContent" in await response.text()
