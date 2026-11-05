from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from aiohttp import FormData
from aiohttp.test_utils import TestClient, TestServer
from telethon import functions, types

from app.db.session import init_db
from app.db.tables import MorningDigest
from app.inbox.digest import _digest_json, preprocess
from app.inbox.service import InboxError, InboxService
from app.inbox.web import create_app
from tests.test_inbox import FakeTelegram, Message, activate

ORIGIN = "http://127.0.0.1:8787"


class Chunks:
    def __init__(self, content, offset):
        self.content, self.offset, self.closed = content, offset, False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.offset >= len(self.content):
            raise StopAsyncIteration
        chunk = self.content[self.offset:self.offset + 4096]
        self.offset += len(chunk)
        return chunk

    async def close(self):
        self.closed = True


class FeatureTelegram(FakeTelegram):
    def __init__(self):
        super().__init__()
        self.requests = []
        self.user = types.User(id=42, access_hash=987654321, first_name="Тестовый",
                               last_name="Контакт", phone="123456", username="test_contact")
        self.body = bytes(range(256)) * 100
        self.streams = []

    async def get_input_entity(self, entity):
        return types.InputPeerUser(42, 987654321)

    async def get_entity(self, peer):
        return self.user

    async def __call__(self, request):
        self.requests.append(request)
        if isinstance(request, functions.messages.GetAvailableReactionsRequest):
            return SimpleNamespace(reactions=[SimpleNamespace(
                reaction=emoji, title=emoji, inactive=False, premium=False)
                for emoji in ("👍", "👎", "❤", "🔥", "🥰", "👏", "😁", "💯", "🤝")])
        if isinstance(request, functions.contacts.GetContactsRequest):
            return SimpleNamespace(users=[self.user])
        if isinstance(request, functions.users.GetFullUserRequest):
            return SimpleNamespace(full_user=SimpleNamespace(
                about="О себе", birthday=types.Birthday(day=3, month=5)))
        if isinstance(request, (functions.messages.SendMessageRequest,
                                functions.messages.SendMediaRequest)):
            return types.UpdateShortSentMessage(901, 0, 0, datetime.now(UTC), out=True)
        if isinstance(request, functions.messages.SendReactionRequest):
            return SimpleNamespace(updates=[])
        return await super().__call__(request)

    def iter_download(self, media, *, offset, request_size, file_size):
        assert 4096 <= request_size <= 512 * 1024
        assert 1048576 % request_size == 0
        assert offset % request_size == 0
        stream = Chunks(self.body, offset)
        self.streams.append(stream)
        return stream


@pytest.fixture()
def feature_service(settings, tmp_path):
    clock = [1_800_000_000.0]
    service = InboxService(FeatureTelegram(), init_db(settings), lambda: set(),
        tmp_path / "media", self_id=1, clock=lambda: clock[0])
    service.test_clock = clock
    service.client.messages = [Message(100, "🙂 один два три")]
    return service


async def headers(client):
    response = await client.get("/api/session")
    return {"Origin": ORIGIN, "X-Inbox-CSRF": (await response.json())["csrf"]}


async def test_quote_uses_utf16_offset_and_native_telegram_reply(feature_service):
    service = feature_service
    row = activate(service, peer="42")
    await service.send(row.id, str(uuid4()), "Мой ответ", reply_to=100,
                       quote_text="два", quote_offset=8)
    request = service.client.requests[-1]
    assert isinstance(request, functions.messages.SendMessageRequest)
    assert request.reply_to.quote_text == "два"
    assert request.reply_to.quote_offset == 8
    assert request.reply_to.reply_to_msg_id == 100
    service.client.send_message.assert_not_called()


@pytest.mark.parametrize(("text", "offset"), [("два", 7), ("подделка", 8), ("", 8),
                                                  ("два", True), ("два", -1)])
async def test_forged_quotes_never_send(feature_service, text, offset):
    service = feature_service
    row = activate(service, peer="42")
    with pytest.raises(InboxError):
        await service.send(row.id, str(uuid4()), "ответ", reply_to=100,
                           quote_text=text, quote_offset=offset)
    assert not service.client.requests
    service.client.send_message.assert_not_called()


async def test_quote_cannot_target_another_forum_topic(feature_service):
    service = feature_service
    row = activate(service, thread=55, forum=True)
    with pytest.raises(InboxError):
        await service.send(row.id, str(uuid4()), "ответ", reply_to=100,
                           quote_text="один", quote_offset=3)
    assert not service.client.requests


async def test_saved_quote_keeps_server_scheduling_and_retry_idempotency(feature_service):
    service = feature_service
    request_id = str(uuid4())
    result = await service.send_saved(request_id, "ответ", reply_to=100,
                                      quote_text="два", quote_offset=8)
    assert result["scheduled_in"] == 12
    assert service.client.requests[-1].schedule_date > datetime.now(UTC)
    await service.send_saved(request_id, "ответ", reply_to=100,
                             quote_text="два", quote_offset=8)
    assert len(service.client.requests) == 1


async def test_multipart_quote_round_trip(feature_service):
    service = feature_service
    row = activate(service, peer="42")
    async with TestClient(TestServer(create_app(service)),
                          headers={"Host": "127.0.0.1:8787"}) as client:
        auth = await headers(client)
        form = FormData()
        for name, value in {"request_id": str(uuid4()), "text": "ответ", "reply_to": "100",
                            "quote_text": "два", "quote_offset": "8"}.items():
            form.add_field(name, value, content_type="text/plain")
        response = await client.post(f"/api/conversations/{row.id}/upload",
                                     data=form, headers=auth)
        assert response.status == 200, await response.text()
    assert service.client.requests[-1].reply_to.quote_offset == 8


async def test_reactions_require_csrf_and_do_not_extend_or_open_access(feature_service):
    service = feature_service
    row = activate(service, peer="42", opened=False)
    async with TestClient(TestServer(create_app(service)),
                          headers={"Host": "127.0.0.1:8787"}) as client:
        path = f"/api/conversations/{row.id}/messages/100/reaction"
        assert (await client.post(path, json={"emoji": "👍"})).status == 403
        auth = await headers(client)
        assert (await client.post(path, json={"emoji": "👍"}, headers=auth)).status == 200
        assert (await client.post(path, json={"emoji": ""}, headers=auth)).status == 200
        assert (await client.post(path, json={"emoji": "⭐"}, headers=auth)).status == 400
    current = service.store.get_any(row.id)
    assert current.opened_at is None and current.expires_at == row.expires_at
    writes = [r for r in service.client.requests
              if isinstance(r, functions.messages.SendReactionRequest)]
    assert len(writes) == 2
    assert writes[-1].reaction == []


async def test_closed_chat_cannot_receive_reaction(feature_service):
    service = feature_service
    row = activate(service, peer="42")
    service.store.close(row.id)
    with pytest.raises(InboxError):
        await service.set_reaction("conversations", row.id, 100, "👍")
    assert not service.client.requests


async def test_contact_tokens_reveal_no_peer_hash_and_expire(feature_service):
    service = feature_service
    result = await service.contacts()
    assert "987654321" not in json.dumps(result)
    row = result["contacts"][0]
    card = await service.contact_profile(row["token"])
    assert card["birthday"] == {"day": 3, "month": 5, "year": None}
    assert card["phone"] == "123456"
    assert not service.store.active()
    assert service.manual_open_status()["remaining"] == 2
    service.test_clock[0] += 601
    with pytest.raises(InboxError):
        await service.contact_profile(row["token"])


async def test_profile_in_chat_does_not_change_timer_and_rejects_foreign_topic(feature_service):
    service = feature_service
    row = activate(service, peer="42")
    card = await service.profile("conversations", row.id)
    assert card["about"] == "О себе"
    assert service.store.get_any(row.id).expires_at == row.expires_at
    forum = activate(service, thread=55, forum=True)
    with pytest.raises(InboxError):
        await service.profile("conversations", forum.id, 100)


def seed_digest(service, source_id="dtest"):
    now = datetime.now(UTC).replace(tzinfo=None)
    with service.store.factory() as session:
        record = MorningDigest(period_start=now, period_end=now, generated_at=now,
            model="test", prompt_version="test", status="success", payload_json=json.dumps({
                "title": "Сводка", "items": [{"title": "Событие", "sources": [{
                    "source_id": source_id, "message_id": 100, "_peer": {
                        "peer_type": "user", "peer_id": "42", "access_hash": "987654321",
                        "display_title": "Источник"}}]}]}))
        session.add(record)
        session.commit()
        return _digest_json(record)


async def test_digest_is_exact_read_only_and_never_activates_a_chat(feature_service):
    service = feature_service
    digest = seed_digest(service)
    assert digest["items"][0]["links"] == ["/?digest_source=dtest&message=100"]
    assert "987654321" not in json.dumps(digest)
    preview = await service.digest_preview("dtest", 100)
    assert preview["message"]["text"] == "🙂 один два три"
    with pytest.raises(InboxError):
        await service.digest_preview("dtest", 101)
    with pytest.raises(InboxError):
        await service.digest_preview("another", 100)
    assert not service.store.active()
    assert service.manual_open_status()["remaining"] == 2
    assert not service.library_snapshots
    assert not service.snapshots


def test_digest_identity_is_persisted_but_never_sent_to_model_context():
    identity = {"peer_type": "user", "peer_id": "42", "access_hash": "987654321"}
    context, refs = preprocess([{"source_id": "test", "message_id": 1,
        "timestamp": "2026-10-30", "source_title": "Источник", "text": "Новый дедлайн",
        "source_peer": identity}])
    assert "987654321" not in json.dumps(context)
    assert next(iter(refs.values()))["_peer"] == identity


@pytest.mark.parametrize(("range_header", "expected", "status"), [
    ("bytes=3-7", bytes(range(3, 8)), 206),
    ("bytes=-4", bytes(range(252, 256)), 206),
    ("bytes=25598-", bytes([254, 255]), 206),
    ("bytes=30000-", b"", 416),
    ("bytes=-0", b"", 416),
    ("bytes=1-2,4-5", b"", 416),
])
async def test_streaming_ranges_and_security_headers(
    feature_service, range_header, expected, status,
):
    service = feature_service
    row = activate(service, peer="42")
    service.client.messages[0].file = SimpleNamespace(
        size=len(service.client.body), mime_type="video/mp4", name="test.mp4")
    service.client.messages[0].media = SimpleNamespace()
    async with TestClient(TestServer(create_app(service)),
                          headers={"Host": "127.0.0.1:8787"}) as client:
        response = await client.get(f"/api/conversations/{row.id}/media/100?stream=1",
                                    headers={"Range": range_header})
        assert response.status == status
        assert await response.read() == expected
        assert response.headers["Cache-Control"] == "no-store"
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        if status == 206:
            assert response.headers["Accept-Ranges"] == "bytes"
            assert service.client.streams[-1].closed
            assert (service.client.streams[-1].offset < len(service.client.body)
                    or "25598" in range_header or "-4" in range_header)
    assert service.store.get_any(row.id).expires_at == row.expires_at


async def test_stream_head_and_expired_access_do_not_download(feature_service):
    service = feature_service
    row = activate(service, peer="42")
    service.client.messages[0].file = SimpleNamespace(
        size=len(service.client.body), mime_type="audio/ogg", name="test.ogg")
    async with TestClient(TestServer(create_app(service)),
                          headers={"Host": "127.0.0.1:8787"}) as client:
        response = await client.head(f"/api/conversations/{row.id}/media/100?stream=1")
        assert response.status == 200
        assert response.headers["Content-Length"] == str(len(service.client.body))
        service.store.close(row.id)
        response = await client.get(f"/api/conversations/{row.id}/media/100?stream=1")
        assert response.status == 404
    assert not service.client.streams


async def test_standard_reaction_catalog_is_cached_and_accepts_more_than_the_short_strip(
    feature_service,
):
    service = feature_service
    row = activate(service, peer="42")
    catalog = await service.available_reactions()
    assert any(item["emoji"] == "💯" for item in catalog["reactions"])
    await service.available_reactions()
    await service.set_reaction("conversations", row.id, 100, "💯")
    reads = [r for r in service.client.requests
             if isinstance(r, functions.messages.GetAvailableReactionsRequest)]
    assert len(reads) == 1
    assert service.client.requests[-1].reaction[0].emoticon == "💯"
    assert service.store.get_any(row.id).expires_at == row.expires_at


@pytest.mark.parametrize("request_name", ["SendReactionRequest", "SendMediaRequest"])
def test_raw_telegram_writes_remain_banned_outside_explicit_manual_boundary(request_name):
    from tests.test_telegram_safety import scan_telegram_safety_source

    source = f"from telethon import functions\nfunctions.messages.{request_name}(None)"
    assert scan_telegram_safety_source(source, "/app/services/background.py")


async def test_quoted_reply_with_attachment_uses_native_send_media(feature_service):
    service = feature_service
    row = activate(service, peer="42")
    attachment = service.upload_dir / "example.txt"
    attachment.write_text("test")
    service.client.upload_file = AsyncMock(return_value=types.InputFile(1, 1, "example.txt", ""))
    await service.send(row.id, str(uuid4()), "ответ с файлом", file_path=attachment,
        filename="example.txt", mime_type="text/plain", reply_to=100,
        quote_text="два", quote_offset=8)
    request = service.client.requests[-1]
    assert isinstance(request, functions.messages.SendMediaRequest)
    assert isinstance(request.media, types.InputMediaUploadedDocument)
    assert request.reply_to.quote_text == "два"
    service.client.send_file.assert_not_called()


async def test_digest_can_show_deselected_source_without_reenabling_its_library(feature_service):
    service = feature_service
    service.store.upsert_library_source(source_id="dtest", peer_type="user", peer_id="42",
        access_hash=987654321, display_title="Источник", library_enabled=False, is_bot=False)
    seed_digest(service)
    assert (await service.digest_preview("dtest", 100))["message"]["id"] == 100
    assert not service.store.library_source("dtest", enabled_only=False).library_enabled
    with pytest.raises(InboxError):
        await service.library_history("dtest")
    assert not service.store.active()


@pytest.mark.parametrize(("size", "span"), [
    (33402, None),
    (135432, None),
    (33402, "bytes=0-"),
    (1600000, "bytes=1048000-1081000"),
    (1600000, "bytes=999999-1500000"),
])
async def test_streaming_uses_telegram_legal_parts_for_voice_and_unaligned_seeks(
    feature_service, size, span,
):
    service = feature_service
    row = activate(service, peer="42")
    service.client.body = (bytes(range(256)) * ((size + 255) // 256))[:size]
    service.client.messages[0].file = SimpleNamespace(
        size=size, mime_type="audio/ogg", name="voice.ogg")
    service.client.messages[0].media = SimpleNamespace()
    async with TestClient(TestServer(create_app(service)),
                          headers={"Host": "127.0.0.1:8787"}) as client:
        response = await client.get(f"/api/conversations/{row.id}/media/100?stream=1",
                                    headers={"Range": span} if span else {})
        assert response.status == (206 if span else 200), await response.text()
        start, stop = (0, size)
        if span:
            left, right = span.removeprefix("bytes=").split("-")
            start, stop = int(left), min(int(right) + 1, size) if right else size
        assert await response.read() == service.client.body[start:stop]
