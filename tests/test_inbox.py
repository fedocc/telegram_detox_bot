from __future__ import annotations

import ast
import base64
import io
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from aiohttp.test_utils import TestClient, TestServer
from PIL import Image
from telethon.errors import ChatWriteForbiddenError
from telethon.tl.types import PeerChannel

from app.db.session import init_db
from app.inbox.service import MAX_CACHE, MAX_MEDIA, InboxError, InboxService, normalize_image
from app.inbox.store import LIFETIME
from app.inbox.web import HOST, PORT, create_app, serve_inbox


class Message(SimpleNamespace):
    def __init__(self, mid, text="context", thread=0, **kwargs):
        super().__init__(id=mid, raw_text=text, out=False, sender_id=2,
            sender=SimpleNamespace(first_name="Никита", last_name=""),
            date=datetime.now(UTC), reply_to=SimpleNamespace(
                reply_to_top_id=thread or None, reply_to_msg_id=thread or None,
                forum_topic=bool(thread)), reply_to_msg_id=thread or None,
            file=None, photo=None, voice=None, audio=None, video=None, video_note=None,
            fwd_from=None,
            action=None)
        self.__dict__.update(kwargs)

    async def get_reply_message(self):
        return getattr(self, "parent", None)


class FakeTelegram:
    def __init__(self):
        self.messages = []
        self.reads = []
        self.connected = True
        self.send_message = AsyncMock(return_value=SimpleNamespace(id=901))
        self.send_file = AsyncMock(return_value=SimpleNamespace(id=902))
        self.downloads = 0

    def is_connected(self):
        return self.connected

    async def get_messages(self, peer, **kwargs):
        self.reads.append((peer, kwargs))
        if "ids" in kwargs:
            return next((m for m in self.messages if m.id == kwargs["ids"]), None)
        messages = self.messages
        if kwargs.get("reply_to"):
            messages = [m for m in messages if m.reply_to.reply_to_top_id == kwargs["reply_to"]]
        if kwargs.get("offset_id"):
            messages = [m for m in messages if m.id < kwargs["offset_id"]]
        if kwargs.get("min_id"):
            messages = [m for m in messages if m.id > kwargs["min_id"]]
        return sorted(messages, key=lambda m: m.id, reverse=not kwargs.get("reverse", False))[
            :kwargs.get("limit", 100)]

    async def download_media(self, message, *, file, progress_callback):
        self.downloads += 1
        progress_callback(message.file.size, message.file.size)
        file.write(b"fake media")


@pytest.fixture()
def service(settings, tmp_path):
    ignored = set()
    clock = [1_800_000_000.0]
    result = InboxService(FakeTelegram(), init_db(settings), lambda: ignored,
                          tmp_path / "media_cache", self_id=1, clock=lambda: clock[0])
    result.test_clock = clock
    result.test_ignored = ignored
    return result


def activate(service, *, thread=0, forum=False, peer="-100123", trigger=100, opened=True):
    row = service.store.activate(peer_id=peer, thread_id=thread, is_forum=forum,
        title="Flare Team", trigger_id=trigger, preview="@fedocc проверь")
    return service.store.open(row.id) if row and opened and not row.manually_closed else row


def image_bytes():
    stream = io.BytesIO()
    Image.new("RGB", (10, 10)).save(stream, format="PNG")
    return stream.getvalue()


@pytest.mark.parametrize("text,outgoing,sender,ignored,expected", [
    ("@fedocc", False, 2, False, True), ("@FEDOCC", False, 2, False, True),
    ("@fedocc_bot", False, 2, False, False), ("@fedoccc", False, 2, False, False),
    ("fedocc", False, 2, False, False), ("обычное", False, 2, False, False),
    ("@fedocc", True, 1, False, False), ("@fedocc", False, 1, False, False),
    ("@fedocc", False, 2, True, False),
])
async def test_activation(service, text, outgoing, sender, ignored, expected):
    if ignored:
        service.test_ignored.add("-100123")
    event = SimpleNamespace(chat_id=-100123, out=outgoing, sender_id=sender, raw_text=text,
        id=100, message=Message(100, text), get_chat=AsyncMock(return_value=SimpleNamespace(
            title="Flare Team", forum=False)))
    await service.observe(event)
    assert bool(service.store.active()) == expected
    service.client.send_message.assert_not_called()
    service.client.send_file.assert_not_called()


async def test_expiry_close_new_mention_and_duplicate(service):
    row = activate(service)
    service.test_clock[0] += LIFETIME - 1
    assert service.store.get(row.id)
    service.test_clock[0] += 1
    assert service.store.active() == []
    assert activate(service, trigger=101).id == row.id
    await service.close(row.id)
    assert service.store.active() == []
    activate(service, trigger=101)
    assert service.store.active() == []
    assert activate(service, trigger=102).id == row.id
    assert service.store.get(row.id)
    service.test_clock[0] += LIFETIME
    await service.cleanup()
    assert service.store.active() == []


@pytest.mark.parametrize("thread", [0, 1, 42])
@pytest.mark.parametrize("photo", [False, True])
async def test_manual_send_routes_and_extends(service, thread, photo):
    row = activate(service, thread=thread, forum=thread > 0)
    service.test_clock[0] += LIFETIME - 100
    rid = str(uuid4())
    result = await service.send(row.id, rid, "текст **без форматирования**",
                                image_bytes() if photo else None)
    method = service.client.send_file if photo else service.client.send_message
    assert method.await_count == 1
    args, kwargs = method.call_args
    assert args[0] == -100123
    assert kwargs["reply_to"] == (thread or None)
    assert kwargs["parse_mode"] is None
    if photo:
        assert args[1].name == "image.jpg"
        assert kwargs["force_document"] is False
    assert service.store.get(row.id).expires_at == service.clock() + LIFETIME
    assert await service.send(row.id, rid, "same retry") == result
    assert method.await_count == 1


async def test_permissions_and_uncertain_send(service):
    row = activate(service)
    expires = row.expires_at
    service.client.send_message.side_effect = ChatWriteForbiddenError(request=None)
    with pytest.raises(InboxError, match="Черновик сохранён"):
        await service.send(row.id, str(uuid4()), "reply")
    assert service.store.get(row.id).expires_at == expires
    service.client.send_message.side_effect = TimeoutError()
    rid = str(uuid4())
    with pytest.raises(InboxError, match="неизвестен"):
        await service.send(row.id, rid, "reply")
    with pytest.raises(InboxError, match="неизвестен"):
        await service.send(row.id, rid, "reply")
    assert service.client.send_message.await_count == 2


async def test_closed_expired_ignored_cannot_send(service):
    row = activate(service)
    service.test_ignored.add(row.peer_id)
    with pytest.raises(InboxError):
        await service.send(row.id, str(uuid4()), "reply")
    service.test_ignored.clear()
    service.test_clock[0] += LIFETIME
    with pytest.raises(InboxError):
        await service.send(row.id, str(uuid4()), "reply")
    service.client.send_message.assert_not_called()


async def test_topic_history_reply_and_media_isolation(service):
    row = activate(service, thread=42, forum=True)
    other = Message(99, "private other topic", 77)
    service.client.messages = [Message(42, action=SimpleNamespace(title="Backend")), other,
        Message(100, "@fedocc", 42, parent=other), Message(101, "follow-up", 42),
        Message(102, "wrong topic", 77)]
    result = await service.history(row.id)
    assert [m["id"] for m in result["messages"]] == [100, 101]
    assert result["messages"][0]["reply"] is None
    assert result["conversation"]["topic_title"] == "Backend"
    with pytest.raises(InboxError):
        await service.media(row.id, 102)
    assert all(options.get("reply_to") == 42 for _, options in service.client.reads
               if "ids" not in options)


async def test_general_topic_paginates_past_other_topics(service):
    row = activate(service, thread=1, forum=True)
    service.client.messages = [Message(100, "@fedocc"),
        *[Message(mid, "other", 42) for mid in range(101, 205)], Message(205, "general")]
    assert [m["id"] for m in (await service.history(row.id))["messages"]] == [100]
    service.test_clock[0] += 3
    assert [m["id"] for m in (await service.history(row.id))["messages"]] == [100, 205]


async def test_discussion_root_and_ordinary_reply(service):
    from app.inbox.service import thread_context

    root = Message(50, fwd_from=SimpleNamespace(from_id=PeerChannel(999), channel_post=22))
    reply = Message(100, parent=root, reply_to_msg_id=50)
    assert await thread_context(reply, SimpleNamespace(forum=False)) == (50, False)
    ordinary = Message(101, parent=Message(99), reply_to_msg_id=99)
    assert await thread_context(ordinary, SimpleNamespace(forum=False)) == (0, False)


async def test_history_includes_before_mention_and_new_self_messages(service):
    row = activate(service)
    service.client.messages = [Message(mid) for mid in range(1, 102)]
    result = await service.history(row.id)
    assert len(result["messages"]) == 101
    service.client.messages.append(Message(102, "own follow-up", out=True))
    service.test_clock[0] += 3
    result = await service.history(row.id)
    assert result["messages"][-1]["own"]
    assert service.store.get(row.id).expires_at == row.expires_at  # Reading never extends.


@pytest.mark.parametrize("kind,mime", [("photo", "image/jpeg"), ("voice", "audio/ogg"),
                                      ("video", "video/mp4"), ("file", "text/html")])
async def test_media_display_safe_cache_cleanup(service, kind, mime):
    row = activate(service)
    message = Message(100, file=SimpleNamespace(name="../../unsafe.html", size=10,
        mime_type=mime, duration=42), **({kind: True} if kind != "file" else {}))
    service.client.messages = [message]
    result = await service.history(row.id)
    assert result["messages"][0]["media"]["kind"] == kind
    path, content_type, _, inline = await service.media(row.id, 100)
    assert path.parent == service.cache_dir
    assert path.name == f"{row.id}_100.bin"
    assert path.stat().st_mode & 0o777 == 0o600
    if kind == "file":
        assert not inline and content_type == "application/octet-stream"
    await service.media(row.id, 100)
    assert service.client.downloads == 1
    await service.close(row.id)
    assert not path.exists()
    with pytest.raises(InboxError):
        await service.media(row.id, 100)


async def test_media_size_and_cache_bounds(service):
    row = activate(service)
    service.client.messages = [Message(100, file=SimpleNamespace(
        name="huge", size=MAX_MEDIA + 1, mime_type="video/mp4", duration=0))]
    await service.history(row.id)
    with pytest.raises(InboxError):
        await service.media(row.id, 100)
    assert service.client.downloads == 0
    path = service.cache_dir / f"{row.id}_1.bin"
    with path.open("wb") as stream:
        stream.truncate(MAX_CACHE)
    await service.cleanup(reserve=1)
    assert not path.exists()


@pytest.mark.parametrize("raw", [b"", b"<svg></svg>", b"GIF89a", b"not image"])
def test_invalid_image(raw):
    with pytest.raises(InboxError):
        normalize_image(raw)


async def test_api_security_and_active_only(service):
    row = activate(service)
    server = TestServer(create_app(service))
    async with TestClient(server, headers={"Host": "127.0.0.1:8787"}) as client:
        response = await client.get("/api/session")
        metadata = await response.json()
        assert metadata["active_minutes"] == 5
        token = metadata["csrf"]
        headers = {"Origin": "http://127.0.0.1:8787", "X-Inbox-CSRF": token}
        response = await client.get("/")
        assert response.status == 200
        page = await response.text()
        assert "Нет активных упоминаний" in page
        assert 'class="titlebar"' not in page
        module = await client.get("/static/playback.mjs")
        assert module.status == 200
        assert "javascript" in module.content_type
        assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
        assert "Access-Control-Allow-Origin" not in response.headers
        for bad in [{"Origin": "https://evil.test"}, {"Origin": "null"},
                    {"Host": "evil.test"}, {"Sec-Fetch-Site": "cross-site"}]:
            assert (await client.get("/api/conversations", headers=bad)).status == 403
        url = f"/api/conversations/{row.id}/send"
        payload = {"request_id": str(uuid4()), "text": "manual"}
        for bad in [{}, {"Origin": headers["Origin"]}, {"X-Inbox-CSRF": token},
                    {**headers, "Origin": "https://evil.test"}]:
            assert (await client.post(url, json=payload, headers=bad)).status == 403
        assert (await client.post(url, data="text", headers=headers)).status == 415
        assert (await client.get(url)).status == 405
        assert (await client.post(url, json={**payload, "voice": "x"},
                                  headers=headers)).status == 400
        assert (await client.post(url.replace("/send", "/voice"), json=payload,
                                  headers=headers)).status == 404
        traversal = f"/api/conversations/{row.id}/media/..%2F..%2Fsecret"
        assert (await client.get(traversal)).status == 404
        assert (await client.get("/static/..%2F..%2F.env")).status == 404
        assert (await client.post(url, json=payload, headers=headers)).status == 200
        image_payload = {"request_id": str(uuid4()), "text": "photo",
                         "image": base64.b64encode(image_bytes()).decode()}
        assert (await client.post(url, json=image_payload, headers=headers)).status == 200
        assert (await client.get("/api/conversations")).status == 200
        data = await (await client.get("/api/conversations")).json()
        assert len(data["conversations"]) == 1
        assert (await client.post(f"/api/conversations/{row.id}/close", json={},
                                  headers=headers)).status == 200
        data = await (await client.get("/api/conversations")).json()
        assert data["conversations"] == []
        assert (await client.get(f"/api/conversations/{row.id}/messages")).status == 404


async def test_server_binds_loopback(service, monkeypatch):
    captured = []
    class Site:
        def __init__(self, runner, host, port):
            captured.append((host, port))

        async def start(self):
            pass

    monkeypatch.setattr("app.inbox.web.web.TCPSite", Site)
    async with serve_inbox(service):
        assert captured == [(HOST, PORT)] == [("127.0.0.1", 8787)]


def test_inbox_never_constructs_or_opens_telegram_session():
    for path in Path("app/inbox").rglob("*.py"):
        tree = ast.parse(path.read_text())
        for item in ast.walk(tree):
            if isinstance(item, ast.Name):
                assert item.id not in {"TelegramClient", "make_client", "SQLiteSession"}
    assert "client, session_factory" in Path("app/telegram/client.py").read_text()


async def test_email_path_survives_inbox_failure(settings, monkeypatch):
    from app.telegram.client import ingest_event
    from tests.fixtures.messages import msg
    from tests.test_mention_only import FakeEmail

    settings = settings.model_copy(update={"mention_only_mode": True})
    email = FakeEmail()
    monkeypatch.setattr("app.telegram.client.event_to_stored_message",
                        AsyncMock(return_value=msg(text="@fedocc")))
    await ingest_event(SimpleNamespace(chat_id="-100123"), settings=settings,
        session_factory=init_db(settings), llm=None, email=email, ignored_chat_ids=set(),
        inbox=SimpleNamespace(observe=AsyncMock(side_effect=RuntimeError("synthetic"))))
    assert len(email.sent) == 1


async def test_runtime_shares_exact_client_and_loop(service, settings, monkeypatch):
    import asyncio
    from contextlib import asynccontextmanager

    from app.telegram import client as runtime

    calls = []
    class Client(FakeTelegram):
        async def connect(self):
            calls.append("connect")

        async def disconnect(self):
            calls.append("disconnect")

        async def is_user_authorized(self):
            return True

        async def get_me(self):
            return SimpleNamespace(id=1)

        def on(self, event):
            return lambda fn: fn

        async def run_until_disconnected(self):
            calls.append(asyncio.get_running_loop())

    client = Client()
    def factory(owner, *args):
        assert owner is client
        service.client = owner
        return service

    @asynccontextmanager
    async def server(inbox):
        assert inbox.client is client
        calls.append(asyncio.get_running_loop())
        yield

    make = lambda settings: client  # noqa: E731
    monkeypatch.setattr(runtime, "make_client", make)
    monkeypatch.setattr("app.inbox.service.InboxService", factory)
    monkeypatch.setattr("app.inbox.web.serve_inbox", server)
    await runtime.run_listener(settings.model_copy(update={"mention_only_mode": True}),
                               service.store.factory, ignored_chat_ids=set(), enable_inbox=True)
    loop = asyncio.get_running_loop()
    assert calls == ["connect", loop, loop, "disconnect"]


async def test_idempotency_survives_runtime_restart(service):
    row = activate(service)
    rid = str(uuid4())
    await service.send(row.id, rid, "reply")
    restarted = InboxService(service.client, service.store.factory, service.store.ignored,
                             service.cache_dir, 1, clock=service.clock)
    assert await restarted.send(row.id, rid, "retry") == {"message_id": 901}
    assert service.client.send_message.await_count == 1


async def test_api_excludes_expired_and_newly_ignored(service):
    first = activate(service)
    second = activate(service, peer="-100456")
    service.test_ignored.add(first.peer_id)
    service.test_clock[0] += LIFETIME + 1
    activate(service, peer=first.peer_id, trigger=101)
    async with TestClient(TestServer(create_app(service)),
                          headers={"Host": "127.0.0.1:8787"}) as client:
        payload = await (await client.get("/api/conversations")).json()
        assert payload["conversations"] == []
        for row in [first, second]:
            assert (await client.get(f"/api/conversations/{row.id}/messages")).status == 404


def test_animated_images_and_pixel_limit_rejected():
    first = Image.new("RGB", (10, 10), "red")
    second = Image.new("RGB", (10, 10), "blue")
    stream = io.BytesIO()
    first.save(stream, format="PNG", save_all=True, append_images=[second])
    with pytest.raises(InboxError, match="Анимированные"):
        normalize_image(stream.getvalue())
    large = io.BytesIO()
    Image.new("1", (5000, 5000)).save(large, format="PNG")
    with pytest.raises(InboxError, match="мегапикселей"):
        normalize_image(large.getvalue())


async def test_invalid_payload_cannot_target_another_peer(service):
    row = activate(service)
    async with TestClient(TestServer(create_app(service)),
                          headers={"Host": "127.0.0.1:8787"}) as client:
        token = (await (await client.get("/api/session")).json())["csrf"]
        headers = {"Origin": "http://127.0.0.1:8787", "X-Inbox-CSRF": token}
        response = await client.post(f"/api/conversations/{row.id}/send", headers=headers,
            json={"request_id": str(uuid4()), "text": "reply", "peer_id": "other"})
        assert response.status == 400
        service.client.send_message.assert_not_called()


async def test_email_and_inbox_activate_together(service, settings, monkeypatch):
    from app.telegram.client import ingest_event
    from tests.fixtures.messages import msg
    from tests.test_mention_only import FakeEmail

    settings = settings.model_copy(update={"mention_only_mode": True})
    email = FakeEmail()
    monkeypatch.setattr("app.telegram.client.event_to_stored_message",
                        AsyncMock(return_value=msg(text="@FEDOCC")))
    event = SimpleNamespace(chat_id=-100123, out=False, sender_id=2, raw_text="@FEDOCC",
        id=100, message=Message(100, "@FEDOCC"),
        get_chat=AsyncMock(return_value=SimpleNamespace(title="Flare Team", forum=False)))
    await ingest_event(event, settings=settings, session_factory=service.store.factory,
                       llm=None, email=email, ignored_chat_ids=set(), inbox=service)
    assert len(email.sent) == 1
    assert len(service.store.active()) == 1


async def test_failed_media_download_never_leaves_a_partial_cache(service):
    row = activate(service)
    service.client.messages = [Message(100, file=SimpleNamespace(
        name="photo.jpg", size=10, mime_type="image/jpeg", duration=0), photo=True)]
    await service.history(row.id)
    service.client.download_media = AsyncMock(side_effect=OSError("synthetic interruption"))
    with pytest.raises(OSError):
        await service.media(row.id, 100)
    assert list(service.cache_dir.iterdir()) == []
    partial = service.cache_dir / "interrupted.part"
    partial.write_bytes(b"partial")
    await service.cleanup()
    assert not partial.exists()


@pytest.mark.parametrize('kind', ['voice', 'audio', 'video_note', 'video', 'photo', 'file'])
async def test_native_telethon_media_classification(service, kind):
    from telethon.tl import types

    row = activate(service)
    attributes = [types.DocumentAttributeFilename('sample.bin')]
    mime = 'application/octet-stream'
    if kind in {'voice', 'audio'}:
        attributes.append(types.DocumentAttributeAudio(42, voice=kind == 'voice'))
        mime = 'audio/ogg'
    elif kind in {'video', 'video_note'}:
        attributes.append(types.DocumentAttributeVideo(42, 320, 320,
                                                       round_message=kind == 'video_note'))
        mime = 'video/mp4'
    media = types.MessageMediaDocument(document=types.Document(
        id=1, access_hash=0, file_reference=b'', date=datetime.now(UTC),
        mime_type=mime, size=100, dc_id=1, attributes=attributes))
    if kind == 'photo':
        media = types.MessageMediaPhoto(photo=types.Photo(
            id=1, access_hash=0, file_reference=b'', date=datetime.now(UTC),
            sizes=[types.PhotoSize('x', 320, 320, 100)], dc_id=1))
    message = types.Message(id=100, peer_id=types.PeerUser(2), from_id=types.PeerUser(2),
                            date=datetime.now(UTC), message='', media=media)
    result = await service.serialize(message, row, {})
    assert result['media']['kind'] == kind
    if kind == 'video_note':
        assert message.video and message.video_note  # Telethon overlaps these properties.


async def test_meaningful_triggers_reset_window_only(service):
    row = activate(service)
    assert row.expires_at - service.clock() == LIFETIME == 300
    for mid, text, parent, expected_reset in [
        (101, 'ordinary', None, False),
        (102, '@fedocc', None, True),
        (103, 'reply', Message(50, out=True, sender_id=1), True),
    ]:
        previous = service.store.get(row.id).expires_at
        service.test_clock[0] += 30
        event = SimpleNamespace(chat_id=-100123, out=False, sender_id=2, raw_text=text,
            id=mid, message=Message(mid, text, parent=parent,
                                   reply_to_msg_id=50 if parent else None),
            get_chat=AsyncMock(return_value=SimpleNamespace(title='Flare Team', forum=False)))
        await service.observe(event)
        assert service.store.get(row.id).expires_at == (
            service.clock() + LIFETIME if expected_reset else previous)


async def test_upgrade_caps_old_windows_without_extending_new_ones(service):
    from app.db.tables import InboxConversation

    row = activate(service)
    other = activate(service, peer='-100456')
    with service.store.factory() as session:
        session.get(InboxConversation, row.id).expires_at = service.clock() + 3600
        session.get(InboxConversation, other.id).expires_at = service.clock() + 40
        session.commit()
    service.store.clamp_existing_lifetimes()
    assert service.store.get(row.id).expires_at == service.clock() + LIFETIME
    assert service.store.get(other.id).expires_at == service.clock() + 40


@pytest.mark.parametrize('text,parent_out,parent_sender,reply,outgoing,sender,ignored,expected', [
    ('@fedocc', False, 9, False, False, 2, False, 'mention_only'),
    ('@FEDOCC', False, 9, False, False, 2, False, 'mention_only'),
    ('@fedocc_bot', False, 9, False, False, 2, False, None),
    ('reply', True, 1, True, False, 2, False, 'direct_reply'),
    ('reply', False, 1, True, False, 2, False, 'direct_reply'),
    ('reply', False, 9, True, False, 2, False, None),
    ('reply', True, 1, True, True, 1, False, None),
    ('reply', True, 1, True, False, 1, False, None),
    ('@fedocc', True, 1, True, False, 2, True, None),
    ('reply', True, 1, True, False, 2, True, None),
    ('@fedocc', True, 1, True, False, 2, False, 'mention_only'),
    ('', True, 1, True, False, 2, False, 'direct_reply'),
])
@pytest.mark.parametrize('thread', [0, 42])
async def test_attention_email_and_inbox_integration(
    service, settings, text, parent_out, parent_sender, reply, outgoing, sender,
    ignored, expected, thread,
):
    from sqlalchemy import select

    from app.db.tables import AlertJob
    from app.db.tables import MessageRecord as StoredRow
    from app.telegram.client import ingest_event
    from tests.test_mention_only import FakeEmail, NeverCalledLLM

    if ignored:
        service.test_ignored.add('-100123')
    message = Message(100, text, thread, out=outgoing, sender_id=sender,
                      reply_to_msg_id=50 if reply else None)
    message.get_reply_message = AsyncMock(return_value=Message(
        50, out=parent_out, sender_id=parent_sender))
    event = SimpleNamespace(chat_id=-100123, id=100, message=message, out=outgoing,
        sender_id=sender, raw_text=text,
        get_chat=AsyncMock(return_value=SimpleNamespace(title='Flare Team', forum=bool(thread))),
        get_sender=AsyncMock(return_value=SimpleNamespace(id=sender, first_name='Test')))
    email = FakeEmail()
    for _ in range(2):
        processed = await ingest_event(event,
            settings=settings.model_copy(update={'mention_only_mode': True}),
            session_factory=service.store.factory, llm=NeverCalledLLM(), email=email,
            ignored_chat_ids=service.test_ignored, inbox=service, self_id=1)
        assert processed is bool(expected)
    assert len(email.sent) == int(bool(expected))
    assert len(service.store.active()) == int(bool(expected))
    assert len(service.store.notifications(0)["events"]) == int(bool(expected))
    with service.store.factory() as session:
        jobs = list(session.scalars(select(AlertJob)))
        assert [job.alert_type for job in jobs] == ([expected] if expected else [])
        assert len(list(session.scalars(select(StoredRow)))) == int(bool(expected))
    if expected:
        assert email.sent[0][0] == 'Telegram alert'
        assert ('Ответ на ваше сообщение' if expected == 'direct_reply'
                else 'Упоминание @fedocc') in email.sent[0][1]
    if ignored or outgoing or sender == 1 or (not reply and not thread):
        message.get_reply_message.assert_not_called()


@pytest.mark.parametrize('parent', [None, RuntimeError('unavailable')])
async def test_unavailable_reply_parent_is_not_a_trigger(parent):
    from app.services.attention import classify_incoming

    message = Message(100, reply_to_msg_id=50)
    message.get_reply_message = AsyncMock(
        side_effect=parent if isinstance(parent, Exception) else None, return_value=parent)
    assert await classify_incoming(message, self_id=1) is None


async def test_pending_survives_listing_history_cleanup_and_new_trigger(service):
    from app.inbox.service import conversation_json

    event = SimpleNamespace(chat_id=-100123, out=False, sender_id=2, raw_text='@fedocc',
        id=100, message=Message(100, '@fedocc'),
        get_chat=AsyncMock(return_value=SimpleNamespace(title='Flare Team', forum=False)))
    await service.observe(event)
    row = service.store.active()[0]
    assert row.opened_at is None
    assert conversation_json(row)['expires_at'] is None
    service.test_clock[0] += 30 * 86400
    await service.cleanup()
    assert service.store.get(row.id).opened_at is None
    await service.history(row.id)
    assert service.store.get(row.id).opened_at is None
    event.id = 101
    event.raw_text = 'new @fedocc'
    await service.observe(event)
    latest = service.store.get(row.id)
    assert latest.opened_at is None and latest.expires_at == 0
    assert latest.trigger_id == 101 and latest.preview == 'new @fedocc'


async def test_first_open_is_idempotent_and_expiry_cannot_reopen(service):
    row = activate(service, opened=False)
    service.test_clock[0] += 10000
    opened = service.store.open(row.id)
    assert opened.opened_at == service.clock()
    assert opened.expires_at == service.clock() + LIFETIME
    service.test_clock[0] += 100
    again = service.store.open(row.id)
    assert again.opened_at == opened.opened_at
    assert again.expires_at == opened.expires_at
    service.test_clock[0] = opened.expires_at
    assert service.store.get(row.id) is None
    assert service.store.open(row.id) is None
    # A fresh attention event starts a new pending cycle, not a countdown.
    fresh = activate(service, trigger=101, opened=False)
    assert fresh.opened_at is None
    assert fresh.expires_at == 0


@pytest.mark.parametrize('opened', [False, True])
async def test_close_pending_or_opened_and_fresh_trigger(service, opened):
    row = activate(service, opened=opened)
    await service.close(row.id)
    assert service.store.get(row.id) is None
    assert service.store.open(row.id) is None
    activate(service, trigger=100, opened=False)
    assert service.store.get(row.id) is None
    fresh = activate(service, trigger=101, opened=False)
    assert fresh.opened_at is None


async def test_pending_open_endpoint_security_and_list_is_read_only(service):
    row = activate(service, opened=False)
    async with TestClient(TestServer(create_app(service)),
                          headers={'Host': '127.0.0.1:8787'}) as client:
        metadata = await (await client.get('/api/session')).json()
        url = f'/api/conversations/{row.id}/open'
        headers = {'Origin': 'http://127.0.0.1:8787', 'X-Inbox-CSRF': metadata['csrf']}
        for _ in range(2):
            listing = await (await client.get('/api/conversations')).json()
            assert listing['conversations'][0]['opened_at'] is None
            assert listing['conversations'][0]['expires_at'] is None
            service.test_clock[0] += LIFETIME + 1
        assert (await client.get(url)).status == 405
        for bad in [{}, {'Origin': headers['Origin']}, {'X-Inbox-CSRF': metadata['csrf']},
                    {**headers, 'Origin': 'https://evil.test'}]:
            assert (await client.post(url, json={}, headers=bad)).status == 403
        assert service.store.get(row.id).opened_at is None
        response = await client.post(url, json={}, headers=headers)
        assert response.status == 200
        opened = (await response.json())['conversation']
        assert opened['opened_at'] == service.clock()
        assert opened['expires_at'] == service.clock() + LIFETIME
        service.test_clock[0] += 60
        again = await (await client.post(url, json={}, headers=headers)).json()
        assert again['conversation']['expires_at'] == opened['expires_at']
        await service.close(row.id)
        assert (await client.post(url, json={}, headers=headers)).status == 404


def test_opened_at_migration_preserves_old_deadlines_and_is_repeatable(settings):
    from sqlalchemy import text

    from app.db.session import make_engine
    from app.db.tables import InboxConversation
    from app.inbox.store import InboxStore

    factory = init_db(settings)
    store = InboxStore(factory, lambda: set(), clock=lambda: 1000)
    row = store.activate(peer_id='2', thread_id=0, is_forum=False,
                         title='Test', trigger_id=1, preview='mention')
    store.open(row.id)
    engine = make_engine(settings)
    with engine.begin() as connection:
        connection.execute(text('ALTER TABLE inbox_conversations DROP COLUMN opened_at'))
    upgraded = init_db(settings)
    with upgraded() as session:
        old = session.get(InboxConversation, row.id)
        assert old.opened_at == 1000
        assert old.expires_at == 1300
    pending = InboxStore(upgraded, lambda: set(), clock=lambda: 1100).activate(
        peer_id='3', thread_id=0, is_forum=False, title='New', trigger_id=1, preview='reply')
    repeated = init_db(settings)
    with repeated() as session:
        assert session.get(InboxConversation, pending.id).opened_at is None
    engine.dispose()


@pytest.mark.parametrize('reason', ['mention_only', 'direct_reply'])
async def test_notification_feed_stable_passive_and_private(service, reason):
    row = service.store.activate(peer_id='-100123', thread_id=0, is_forum=False,
        title='Team', trigger_id=100, preview=' hello\n\tworld ' + 'x' * 400, reason=reason)
    first = service.store.notifications(0)
    assert len(first['events']) == 1
    event = first['events'][0]
    assert event['conversation_id'] == row.id
    assert event['trigger_reason'] == ('mention' if reason == 'mention_only' else 'direct_reply')
    assert len(event['preview']) == 240 and '\n' not in event['preview']
    assert set(event) == {'event_id', 'conversation_id', 'title', 'topic_title', 'preview',
                          'trigger_reason', 'created_at'}
    assert service.store.notifications(0) == first
    assert service.store.notifications(first['cursor'])['events'] == []
    assert service.store.notifications() == {'events': [], 'cursor': first['cursor']}
    assert service.store.get(row.id).opened_at is None
    service.store.activate(peer_id='-100123', thread_id=0, is_forum=False,
        title='Team', trigger_id=100, preview='replayed', reason=reason)
    assert service.store.notifications(0) == first
    service.store.close(row.id)
    assert service.store.notifications(0) == {'events': [], 'cursor': first['cursor']}


async def test_notification_feed_endpoint_and_invalid_cursor(service):
    service.store.activate(peer_id='-100123', thread_id=0, is_forum=False,
        title='Team', trigger_id=100, preview='@fedocc', reason='mention_only')
    async with TestClient(TestServer(create_app(service)),
                          headers={'Host': '127.0.0.1:8787'}) as client:
        assert (await (await client.get('/api/notifications')).json())['events'] == []
        response = await client.get('/api/notifications?after=0')
        assert len((await response.json())['events']) == 1
        assert service.store.active()[0].opened_at is None
        for invalid in ['-1', 'NaN', '9' * 30, '1.2']:
            assert (await client.get('/api/notifications?after=' + invalid)).status == 400
        forbidden = await client.get('/api/notifications',
                                     headers={'Origin': 'https://evil.test'})
        assert forbidden.status == 403
        response = await client.get('/api/notifications?after=0')
        assert response.headers['Cache-Control'] == 'no-store'


async def test_feed_cursor_pagination_ignored_retention_and_backend_restart(service):
    from app.inbox.store import InboxStore

    for mid in range(1, 104):
        service.store.activate(peer_id='-100123', thread_id=0, is_forum=False,
            title='Team', trigger_id=mid, preview='@fedocc', reason='mention_only')
    first = service.store.notifications(0)
    assert len(first['events']) == 100
    restarted = InboxStore(service.store.factory, lambda: service.test_ignored, service.clock)
    tail = restarted.notifications(first['cursor'])
    assert len(tail['events']) == 3
    service.test_ignored.add('-100123')
    assert restarted.notifications(first['cursor'])['events'] == []
    service.test_ignored.clear()
    service.test_clock[0] += 86401
    await service.cleanup()
    assert restarted.notifications(0)['events'] == []
    # Pending conversation remains, but old notification bodies are redacted.
    assert service.store.active()[0].opened_at is None
    from sqlalchemy import select

    from app.db.tables import InboxNotification

    with service.store.factory() as session:
        assert all(row.preview == '' for row in session.scalars(select(InboxNotification)))


def test_feed_keeps_distinct_out_of_order_triggers_without_rewinding_lifecycle(service):
    row = service.store.activate(peer_id='-100123', thread_id=0, is_forum=False,
        title='Team', trigger_id=102, preview='newer mention', reason='mention_only')
    for _ in range(2):
        service.store.activate(peer_id='-100123', thread_id=0, is_forum=False,
            title='Team', trigger_id=101, preview='resolved reply', reason='direct_reply')
    events = service.store.notifications(0)['events']
    assert [e['trigger_reason'] for e in events] == ['mention', 'direct_reply']
    assert service.store.get(row.id).trigger_id == 102
    assert service.store.get(row.id).opened_at is None
