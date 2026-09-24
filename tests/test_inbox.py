from __future__ import annotations

import ast
import asyncio
import base64
import io
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from aiohttp import FormData
from aiohttp.test_utils import TestClient, TestServer
from PIL import Image
from telethon.errors import ChatWriteForbiddenError
from telethon.tl.types import (
    DocumentAttributeCustomEmoji,
    InputStickerSetEmpty,
    MessageEntityCustomEmoji,
    MessageEntityTextUrl,
    MessageReactions,
    PeerChannel,
    ReactionCount,
    ReactionCustomEmoji,
    ReactionEmoji,
    ReactionPaid,
)

from app.db.session import init_db
from app.inbox.service import (
    MAX_CACHE,
    MAX_MEDIA,
    InboxError,
    InboxService,
    normalize_image,
    rich_text_segments,
)
from app.inbox.store import LIFETIME, InboxStore
from app.inbox.web import HOST, PORT, create_app, serve_inbox


class Message(SimpleNamespace):
    def __init__(self, mid, text="context", thread=0, **kwargs):
        super().__init__(id=mid, raw_text=text, out=False, sender_id=2,
            sender=SimpleNamespace(first_name="Никита", last_name=""),
            date=datetime.now(UTC), reply_to=SimpleNamespace(
                reply_to_top_id=thread or None, reply_to_msg_id=thread or None,
                forum_topic=bool(thread)), reply_to_msg_id=thread or None,
            file=None, photo=None, voice=None, audio=None, video=None, video_note=None,
            fwd_from=None, entities=None, pinned=False,
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
        self.custom_emoji_calls = []
        self.unresolved_custom_emoji = set()

    async def __call__(self, request):
        self.custom_emoji_calls.append(list(request.document_id))
        return [SimpleNamespace(id=value, mime_type="video/webm", size=100,
            attributes=[DocumentAttributeCustomEmoji("🔥", InputStickerSetEmpty())])
            for value in request.document_id if value not in self.unresolved_custom_emoji]

    def is_connected(self):
        return self.connected

    async def get_messages(self, peer, **kwargs):
        self.reads.append((peer, kwargs))
        if "ids" in kwargs:
            return next((m for m in self.messages if m.id == kwargs["ids"]), None)
        messages = self.messages
        if "filter" in kwargs:
            messages = [m for m in messages if getattr(m, "pinned", False)]
        if kwargs.get("search"):
            needle = kwargs["search"].casefold()
            messages = [m for m in messages if needle in (m.raw_text or "").casefold()]
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


def test_custom_emoji_segments_are_utf16_safe_and_preserve_links():
    text = "👨‍💻 🔥 hello https://example.com"
    # The custom entity begins after the five UTF-16 code units in 👨‍💻 plus a space.
    entities = [MessageEntityCustomEmoji(offset=6, length=2, document_id=42),
                MessageEntityTextUrl(offset=9, length=5, url="https://example.org")]
    segments = rich_text_segments(text, entities, {42: {
        "document_id": "42", "format": "video", "available": True,
        "url": "/api/custom-emoji/42", "text_color": False,
    }})
    assert "".join(item["text"] for item in segments) == text
    custom = next(item for item in segments if "custom_emoji" in item)
    assert custom["text"] == "🔥" and custom["custom_emoji"]["document_id"] == "42"
    assert next(item for item in segments if item.get("url"))["text"] == "hello"


def test_unresolved_and_multiple_custom_emoji_keep_unicode_fallback():
    text = "🔥 hello 🫠 world"
    segments = rich_text_segments(text, [
        MessageEntityCustomEmoji(offset=0, length=2, document_id=1),
        MessageEntityCustomEmoji(offset=9, length=2, document_id=2),
    ])
    assert "".join(item["text"] for item in segments) == text
    assert [item["text"] for item in segments if "custom_emoji" in item] == ["🔥", "🫠"]
    assert all(not item["custom_emoji"]["available"] for item in segments
               if "custom_emoji" in item)


async def test_custom_emoji_documents_are_batch_resolved_and_cached(service):
    messages = [Message(1, "🔥", entities=[MessageEntityCustomEmoji(0, 2, 11)]),
                Message(2, "🫠", entities=[MessageEntityCustomEmoji(0, 2, 12)])]
    row = activate(service)
    first = await service.serialize_many(messages, row, {item.id: item for item in messages})
    second = await service.serialize_many(messages, row, {item.id: item for item in messages})
    assert service.client.custom_emoji_calls == [[11, 12]]
    assert first == second
    assert all(item["segments"][0]["custom_emoji"]["format"] == "video" for item in first)


async def test_reactions_serialize_counts_without_identities_or_extra_rpc(service):
    message = Message(1, "hello", reactions=MessageReactions(results=[
        ReactionCount(ReactionEmoji("👍"), 4),
        ReactionCount(ReactionEmoji("❤️"), 2),
        ReactionCount(ReactionPaid(), 1),
        ReactionCount(ReactionEmoji("🔥"), 0),
    ], recent_reactions=[SimpleNamespace(peer_id=PeerChannel(999))]))
    result = (await service.serialize_many([message], activate(service), {1: message}))[0]

    assert result["reactions"] == [
        {"type": "emoji", "emoji": "👍", "count": 4},
        {"type": "emoji", "emoji": "❤️", "count": 2},
        {"type": "paid", "emoji": "⭐", "count": 1},
    ]
    assert "999" not in str(result)
    assert service.client.custom_emoji_calls == []


async def test_custom_reactions_share_batch_resolution_and_cache_with_entities(service):
    messages = [
        Message(1, "hello", reactions=MessageReactions(results=[
            ReactionCount(ReactionCustomEmoji(22), 3),
        ])),
        Message(2, "🔥", entities=[MessageEntityCustomEmoji(0, 2, 11)],
                reactions=MessageReactions(results=[
                    ReactionCount(ReactionCustomEmoji(22), 2),
                ])),
    ]
    row = activate(service)
    first = await service.serialize_many(messages, row, {item.id: item for item in messages})
    second = await service.serialize_many(messages, row, {item.id: item for item in messages})

    assert service.client.custom_emoji_calls == [[11, 22]]
    assert first == second
    assert first[0]["reactions"][0]["custom_emoji"]["available"] is True


async def test_unresolved_custom_reaction_keeps_visible_fallback(service):
    service.client.unresolved_custom_emoji.add(404)
    message = Message(1, "hello", reactions=MessageReactions(results=[
        ReactionCount(ReactionCustomEmoji(404), 2),
    ]))
    result = (await service.serialize_many([message], activate(service), {1: message}))[0]
    assert result["reactions"][0]["emoji"] == "◉"
    assert result["reactions"][0]["custom_emoji"]["available"] is False


def test_library_config_is_static_ordered_and_fail_closed(tmp_path):
    from app.inbox.library import load_library_chats

    path = tmp_path / "library.json"
    path.write_text('{"chats":[{"peer_id":-10022,"title":"Team"},'
                    '{"peer_id":-10022,"title":"Duplicate"},'
                    '{"peer_id":"oops","title":"Bad"}]}', encoding="utf-8")
    rows = load_library_chats(path)
    assert [(row.id, row.title, row.writable) for row in rows] == [
        ("saved", "Избранное", True), ("n10022", "Team", False),
    ]
    path.write_text("not json", encoding="utf-8")
    assert [row.id for row in load_library_chats(path)] == ["saved"]


def test_static_library_is_seeded_persistently_without_dialog_scan(settings, tmp_path):
    from app.inbox.library import SAVED_MESSAGES, LibraryChat

    factory = init_db(settings)
    configured = LibraryChat("n1000000000022", -1000000000022, "Course")
    from app.inbox.store import InboxStore

    existing = InboxStore(factory, lambda: set()).activate(
        peer_id=configured.peer_id, peer_type="channel", thread_id=0,
        is_forum=False, title="Course", trigger_id=1, preview="lesson",
        reason="mention_only",
    )
    first_client = FakeTelegram()
    first_client.iter_dialogs = AsyncMock(side_effect=AssertionError("startup scan"))
    first = InboxService(
        first_client, factory, lambda: set(), tmp_path / "first-cache", self_id=1,
        library=(SAVED_MESSAGES, configured),
    )
    assert [row["title"] for row in first.library_json()] == ["Избранное", "Course"]
    assert first.store.get(existing.id).library_source_id == configured.id
    first_client.iter_dialogs.assert_not_called()

    restarted = InboxService(
        FakeTelegram(), factory, lambda: set(), tmp_path / "second-cache", self_id=1,
        library=(SAVED_MESSAGES,),
    )
    payload = restarted.library_json()
    assert [row["id"] for row in payload] == ["saved", configured.id]
    assert all(
        "peer_id" not in row and "peer_type" not in row and "access_hash" not in row
        for row in payload
    )


async def test_library_is_paginated_read_only_and_does_not_touch_lifecycle(service):
    from app.inbox.library import SAVED_MESSAGES, LibraryChat

    service.library = (SAVED_MESSAGES, LibraryChat("n10022", -10022, "Team"))
    service.library_by_id = {row.id: row for row in service.library}
    service.client.messages = [Message(mid, f"message {mid}") for mid in range(1, 61)]
    pending = activate(service, opened=False)
    first = await service.library_history("n10022")
    assert len(first["messages"]) == 50 and first["next_before"] == 11
    second = await service.library_history("n10022", first["next_before"])
    assert [item["id"] for item in second["messages"]] == list(range(1, 11))
    assert service.store.get(pending.id).opened_at is None
    assert service.library_json()[0] == {
        "id": "saved", "title": "Избранное", "writable": True,
        "notifications_muted": False, "allow_bot_write": False,
        "digest_excluded": False, "sort_order": 0, "is_bot": False,
    }
    assert all("peer" not in key for row in service.library_json() for key in row)


async def test_saved_messages_use_server_schedule_without_status_rpc(service, tmp_path):
    result = await service.send_saved(str(uuid4()), "remember this")
    assert result["scheduled_in"] == 12
    args, kwargs = service.client.send_message.await_args
    assert args == ("me", "remember this")
    assert kwargs["schedule"].total_seconds() == 12
    assert not hasattr(service.client, "update_status")
    document = tmp_path / "report.pdf"
    document.write_bytes(b"x" * (2 * 1024 * 1024))
    result = await service.send_saved(str(uuid4()), "file", file_path=document,
                                      filename="report.pdf", mime_type="application/pdf")
    assert result["scheduled_in"] == 19
    assert service.client.send_file.await_args.kwargs["force_document"] is True


async def test_reply_and_generic_file_are_bound_to_active_conversation(service, tmp_path):
    row = activate(service)
    target = Message(88, "incoming")
    service.client.messages = [target]
    document = tmp_path / "note.txt"
    document.write_text("hello", encoding="utf-8")
    await service.send(row.id, str(uuid4()), "answer", reply_to=88,
                       file_path=document, filename="note.txt", mime_type="text/plain")
    kwargs = service.client.send_file.await_args.kwargs
    assert kwargs["reply_to"] == 88 and kwargs["force_document"] is True
    with pytest.raises(InboxError, match="не принадлежит"):
        await service.send(row.id, str(uuid4()), "bad", reply_to=999)


async def test_multipart_upload_is_bounded_safe_and_cleaned(service):
    row = activate(service)
    async with TestClient(TestServer(create_app(service)),
                          headers={"Host": "127.0.0.1:8787"}) as client:
        token = (await (await client.get("/api/session")).json())["csrf"]
        headers = {"Origin": "http://127.0.0.1:8787", "X-Inbox-CSRF": token}
        form = FormData()
        form.add_field("request_id", str(uuid4()))
        form.add_field("text", "document")
        form.add_field("file", b"content", filename="../../note.txt",
                       content_type="text/plain")
        response = await client.post(f"/api/conversations/{row.id}/upload",
                                     data=form, headers=headers)
        assert response.status == 200
        attributes = service.client.send_file.await_args.kwargs["attributes"]
        assert attributes[0].file_name == "note.txt"
        assert list(service.upload_dir.glob("*.upload")) == []
        assert (await client.post("/api/library/n123/send", json={},
                                  headers=headers)).status == 404


async def test_multipart_over_configured_limit_never_reaches_telegram(service):
    row = activate(service)
    service.upload_max = 4
    async with TestClient(TestServer(create_app(service)),
                          headers={"Host": "127.0.0.1:8787"}) as client:
        token = (await (await client.get("/api/session")).json())["csrf"]
        form = FormData()
        form.add_field("request_id", str(uuid4()))
        form.add_field("text", "file")
        form.add_field("file", b"12345", filename="large.bin",
                       content_type="application/octet-stream")
        response = await client.post(f"/api/conversations/{row.id}/upload", data=form,
            headers={"Origin": "http://127.0.0.1:8787", "X-Inbox-CSRF": token})
        assert response.status == 413
        service.client.send_file.assert_not_called()
        assert list(service.upload_dir.glob("*.upload")) == []


async def test_saved_unknown_delivery_state_is_not_retried(service):
    service.client.send_message.side_effect = OSError("network ended")
    request_id = str(uuid4())
    with pytest.raises(InboxError, match="неизвестен"):
        await service.send_saved(request_id, "once")
    with pytest.raises(InboxError, match="неизвестен"):
        await service.send_saved(request_id, "once")
    assert service.client.send_message.await_count == 1


async def test_private_trigger_reopens_and_extends_only_active_window(service):
    row = service.store.activate(peer_id="2", thread_id=0, is_forum=False,
        title="Nikita", trigger_id=1, preview="first", reason="private_message")
    assert row.opened_at is None and row.expires_at == 0
    opened = service.store.open(row.id)
    assert service.store.refresh_view_lease(row.id)
    service.test_clock[0] += 20
    assert service.store.refresh_view_lease(row.id)
    extended = service.store.activate(peer_id="2", thread_id=0, is_forum=False,
        title="Nikita", trigger_id=2, preview="second", reason="private_message")
    assert extended.expires_at == service.clock() + LIFETIME
    service.test_clock[0] = extended.expires_at
    reopened = service.store.activate(peer_id="2", thread_id=0, is_forum=False,
        title="Nikita", trigger_id=3, preview="third", reason="private_message")
    assert reopened.opened_at is None and reopened.expires_at == 0
    assert reopened.id == opened.id


@pytest.mark.parametrize("private_human,expected", [
    (True, "private_message"), (False, None),
])
async def test_private_human_trigger_and_priority(private_human, expected):
    from app.services.attention import classify_incoming

    ordinary = Message(1, "hello")
    assert await classify_incoming(ordinary, self_id=1,
                                   private_human=private_human) == expected
    mention = Message(2, "@fedocc hello")
    assert await classify_incoming(mention, self_id=1,
                                   private_human=private_human) == "mention_only"


async def test_private_human_activates_inbox_and_notification_without_email(
        service, settings, monkeypatch):
    from sqlalchemy import select

    from app.db.tables import AlertJob, MessageRecord
    from app.telegram.client import ingest_event
    from tests.fixtures.messages import msg
    from tests.test_mention_only import FakeEmail

    monkeypatch.setattr("app.telegram.client.event_to_stored_message",
                        AsyncMock(return_value=msg(text="ordinary private")))
    event = SimpleNamespace(
        chat_id=2, sender_id=2, out=False, raw_text="ordinary private", id=44,
        message=Message(44, "ordinary private"),
        get_sender=AsyncMock(return_value=SimpleNamespace(id=2, bot=False)),
        get_chat=AsyncMock(return_value=SimpleNamespace(
            id=2, first_name="Nikita", last_name="", forum=False)),
    )
    email = FakeEmail()
    assert await ingest_event(
        event, settings=settings.model_copy(update={"mention_only_mode": True}),
        session_factory=service.store.factory, llm=None, email=email,
        ignored_chat_ids=set(), inbox=service, self_id=1,
    )
    assert email.sent == []
    assert service.store.notifications(0)["events"][0]["trigger_reason"] == "private_message"
    assert service.store.active()[0].opened_at is None
    with service.store.factory() as session:
        assert len(list(session.scalars(select(MessageRecord)))) == 1
        assert list(session.scalars(select(AlertJob))) == []
    event.id = 45
    event.message = Message(45, "@fedocc from bot")
    event.get_sender = AsyncMock(return_value=SimpleNamespace(id=9, bot=True))
    assert not await ingest_event(
        event, settings=settings.model_copy(update={"mention_only_mode": True}),
        session_factory=service.store.factory, llm=None, email=email,
        ignored_chat_ids=set(), inbox=service, self_id=1,
    )
    assert len(service.store.notifications(0)["events"]) == 1


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
    async def server(inbox, **kwargs):
        assert inbox.client is client
        assert kwargs["allowed_origins"] == ("http://127.0.0.1:8787",)
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


async def test_inbox_media_flood_wait_cleans_partial_and_blocks_other_peers(service):
    from telethon.errors import FloodWaitError

    row = activate(service)
    message = Message(100, file=SimpleNamespace(
        name="photo.jpg", size=10, mime_type="image/jpeg", duration=0,
    ), photo=True)
    service.client.messages = [message]
    await service.history(row.id)
    calls = 0

    async def flood_wait(message, *, file, progress_callback):
        nonlocal calls
        calls += 1
        file.write(b"partial")
        raise FloodWaitError(request=None, capture=11)

    service.client.download_media = flood_wait
    with pytest.raises(InboxError) as failure:
        await service.media(row.id, message.id)
    assert failure.value.status == 429 and failure.value.retry_after == 11
    assert calls == 1 and list(service.cache_dir.iterdir()) == []

    reads = list(service.client.reads)
    with pytest.raises(InboxError) as cooldown:
        await service.library_history("saved")
    assert cooldown.value.status == 429 and cooldown.value.retry_after == 11
    assert service.client.reads == reads and calls == 1


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


@pytest.mark.parametrize(("mime", "attributes", "sticker_format"), [
    ("image/webp", [], "static"),
    ("application/x-tgsticker", ["animated"], "animated"),
    ("video/webm", ["video"], "video"),
    ("application/octet-stream", [], "unsupported"),
])
async def test_stickers_with_empty_text_are_preserved_by_document_attributes(
        service, mime, attributes, sticker_format):
    from telethon.tl import types

    row = activate(service)
    document_attributes = [
        types.DocumentAttributeSticker("🙂", types.InputStickerSetEmpty()),
        types.DocumentAttributeImageSize(512, 512),
    ]
    if "animated" in attributes:
        document_attributes.append(types.DocumentAttributeAnimated())
    if "video" in attributes:
        document_attributes.append(types.DocumentAttributeVideo(2, 512, 512))
    media = types.MessageMediaDocument(document=types.Document(
        id=1, access_hash=0, file_reference=b'', date=datetime.now(UTC),
        mime_type=mime, size=100, dc_id=1, attributes=document_attributes,
    ))
    message = types.Message(
        id=100, peer_id=types.PeerUser(2), from_id=types.PeerUser(2),
        date=datetime.now(UTC), message='', media=media,
    )

    result = await service.serialize(message, row, {})

    assert result["text"] == ""
    assert result["media"]["kind"] == "sticker"
    assert result["media"]["sticker_format"] == sticker_format


async def test_service_add_user_uses_batch_resolved_names(service):
    from telethon.tl import types

    row = activate(service)
    message = Message(101, "", action=types.MessageActionChatAddUser([3]))
    message._action_entities = [SimpleNamespace(first_name="Пётр", last_name="Петров")]

    result = await service.serialize(message, row, {})

    assert result["system"] == "Никита добавил участника: Пётр Петров"
    assert result["text"] == "" and result["media"] is None


@pytest.mark.parametrize(("action", "expected"), [
    ("link", "Пётр присоединился по ссылке"),
    ("request", "Пётр присоединился по запросу"),
])
async def test_service_join_events_are_readable(service, action, expected):
    from telethon.tl import types

    row = activate(service)
    kind = (types.MessageActionChatJoinedByLink(inviter_id=9) if action == "link"
            else types.MessageActionChatJoinedByRequest())
    message = Message(101, "", action=kind, sender_id=3,
                      sender=SimpleNamespace(first_name="Пётр", last_name=""))

    assert (await service.serialize(message, row, {}))["system"] == expected


@pytest.mark.parametrize(("sender_id", "sender", "expected"), [
    (3, "Пётр", "Пётр покинул группу"),
    (2, "Иван", "Иван удалил участника: Пётр"),
])
async def test_service_leave_and_remove_events_are_readable(
        service, sender_id, sender, expected):
    from telethon.tl import types

    row = activate(service)
    message = Message(
        101, "", action=types.MessageActionChatDeleteUser(user_id=3),
        sender_id=sender_id, sender=SimpleNamespace(first_name=sender, last_name=""),
    )
    message._action_entities = [SimpleNamespace(first_name="Пётр", last_name="")]

    assert (await service.serialize(message, row, {}))["system"] == expected


@pytest.mark.parametrize(("action", "expected"), [
    pytest.param("title", "Никита изменил название на «Новый проект»", id="title"),
    pytest.param("photo", "Никита удалил фото группы", id="photo"),
    pytest.param("pin", "Никита закрепил сообщение", id="pin"),
])
async def test_service_title_photo_and_pin_events_are_system_rows(
        service, action, expected):
    from telethon.tl import types

    row = activate(service)
    actions = {
        "title": types.MessageActionChatEditTitle("Новый проект"),
        "photo": types.MessageActionChatDeletePhoto(),
        "pin": types.MessageActionPinMessage(),
    }
    result = await service.serialize(Message(101, "", action=actions[action]), row, {})

    assert result["system"] == expected


async def test_unknown_service_action_never_becomes_empty_bubble(service):
    from telethon.tl import types

    row = activate(service)
    result = await service.serialize(
        Message(101, "", action=types.MessageActionEmpty()), row, {}
    )

    assert result["system"] == "Системное событие"
    assert result["text"] == ""


async def test_normal_empty_message_is_not_serialized(service):
    row = activate(service)

    assert await service.serialize(Message(101, ""), row, {}) is None


async def test_history_omits_blank_content_but_keeps_service_rows(service):
    from telethon.tl import types

    row = activate(service, trigger=100)
    service_message = Message(100, "", action=types.MessageActionPinMessage())
    service.client.messages = [Message(99, ""), service_message]

    result = await service.history(row.id)

    assert [(item["id"], item["system"]) for item in result["messages"]] == [
        (100, "Никита закрепил сообщение")
    ]


async def test_meaningful_triggers_reset_window_only(service):
    row = activate(service)
    assert service.store.refresh_view_lease(row.id)
    assert row.expires_at - service.clock() == LIFETIME == 300
    for mid, text, parent, expected_reset in [
        (101, 'ordinary', None, False),
        (102, '@fedocc', None, True),
        (103, 'reply', Message(50, out=True, sender_id=1), True),
    ]:
        previous = service.store.get(row.id).expires_at
        service.test_clock[0] += 30
        assert service.store.refresh_view_lease(row.id)
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
                          'trigger_reason', 'unread_count', 'created_at'}
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


async def test_private_reply_metadata_uses_one_canonical_conversation(service):
    from telethon.tl.types import InputPeerUser

    event = SimpleNamespace(
        chat_id=2,
        sender_id=2,
        out=False,
        raw_text="reply",
        id=10,
        message=Message(10, "reply", thread=7),
        get_chat=AsyncMock(return_value=SimpleNamespace(
            id=2, first_name="Nikita", last_name="", forum=False,
        )),
        get_input_chat=AsyncMock(return_value=InputPeerUser(2, 123456)),
    )
    await service.observe(event, trigger="private_message")
    event.id = 11
    event.message = Message(11, "plain")
    await service.observe(event, trigger="private_message")

    rows = service.store.active()
    assert len(rows) == 1
    assert rows[0].peer_type == "user"
    assert rows[0].thread_id == 0
    assert rows[0].access_hash == "123456"
    assert rows[0].unread_count == 2


def test_local_unread_is_persistent_deduplicated_and_open_suppresses_feed(service):
    row = None
    for message_id in range(1, 21):
        row = service.store.activate(
            peer_id="2", peer_type="user", thread_id=999, is_forum=False,
            title="Nikita", trigger_id=message_id, preview=str(message_id),
            reason="private_message",
        )
    assert row.thread_id == 0
    assert row.unread_count == 20
    opened = service.store.open(row.id)
    assert opened.unread_count == 0
    assert opened.last_seen_message_id == 20
    suppressed = service.store.notifications(0)
    assert suppressed["events"] == [] and suppressed["cursor"] == 20

    for message_id in (22, 21, 22):
        service.store.activate(
            peer_id="2", peer_type="user", thread_id=0, is_forum=False,
            title="Nikita", trigger_id=message_id, preview=str(message_id),
            reason="private_message",
        )
    updated = service.store.get(row.id)
    assert updated.latest_relevant_message_id == 22
    assert updated.unread_count == 2


def test_foreground_lease_keeps_new_attention_open_and_restart_loses_lease(service):
    row = service.store.activate(
        peer_id="2", peer_type="user", thread_id=0, is_forum=False,
        title="Nikita", trigger_id=1, preview="one", reason="private_message",
    )
    service.store.open(row.id)
    assert service.store.refresh_view_lease(row.id)
    service.test_clock[0] += 5
    viewed = service.store.activate(
        peer_id="2", peer_type="user", thread_id=0, is_forum=False,
        title="Nikita", trigger_id=2, preview="two", reason="private_message",
    )
    assert viewed.opened_at is not None
    assert viewed.expires_at == service.clock() + LIFETIME
    assert viewed.unread_count == 0 and viewed.last_seen_message_id == 2
    assert service.store.notifications(0)["events"] == []

    restarted = InboxStore(service.store.factory, lambda: set(), service.clock)
    service.test_clock[0] += 5
    pending = restarted.activate(
        peer_id="2", peer_type="user", thread_id=0, is_forum=False,
        title="Nikita", trigger_id=3, preview="three", reason="private_message",
    )
    assert pending.opened_at is None and pending.expires_at == 0
    assert pending.unread_count == 1


async def test_foreground_lease_endpoint_and_history_get_are_separate(service):
    row = activate(service, opened=False)
    async with TestClient(TestServer(create_app(service)),
                          headers={"Host": "127.0.0.1:8787"}) as client:
        token = (await (await client.get("/api/session")).json())["csrf"]
        headers = {"Origin": "http://127.0.0.1:8787", "X-Inbox-CSRF": token}
        await client.post(f"/api/conversations/{row.id}/open", json={}, headers=headers)
        await client.get(f"/api/conversations/{row.id}/messages")
        assert not service.store.has_view_lease(row.id)
        assert (await client.post(
            f"/api/conversations/{row.id}/lease", json={}, headers=headers,
        )).status == 200
        assert service.store.has_view_lease(row.id)
        assert (await client.post(
            f"/api/conversations/{row.id}/lease/release", json={}, headers=headers,
        )).status == 200
        assert not service.store.has_view_lease(row.id)


async def test_manual_open_quota_is_global_persistent_repeatable_and_resets(service):
    dialog_key = "manual-token"
    service.dialog_tokens[dialog_key] = {
        "purpose": "manual_open", "peer_type": "user", "peer_id": "40",
        "access_hash": 4000, "display_title": "Person", "is_bot": False,
        "can_write": True, "source_id": None, "expires": service.clock() + 300,
    }
    first = await service.open_manual_write(dialog_key)
    assert first["quota"]["used"] == 1 and first["quota"]["remaining"] == 1
    assert first["source"]["access_until"] == service.clock() + LIFETIME
    source_id = first["source"]["id"]

    attention = service.store.activate(
        peer_id="41", peer_type="user", thread_id=0, is_forum=False,
        title="Incoming", trigger_id=1, preview="hello", reason="private_message",
    )
    assert attention is not None and service.manual_open_status()["used"] == 1

    service.test_clock[0] += 20
    result = await service.send_manual_write(source_id, str(uuid4()), "hello")
    assert result == {"message_id": 901}
    assert service.manual_write_source(source_id).manual_access_until == service.clock() + LIFETIME

    second = await service.open_manual_write(dialog_key)
    assert second["source"]["id"] == source_id
    assert second["quota"]["used"] == 2 and second["quota"]["remaining"] == 0
    with pytest.raises(InboxError) as exhausted:
        await service.open_manual_write(dialog_key)
    assert exhausted.value.status == 429

    restarted = InboxStore(service.store.factory, lambda: set(), service.clock)
    assert restarted.manual_open_status(service.timezone)["used"] == 2
    service.test_clock[0] += 24 * 60 * 60
    service.dialog_tokens[dialog_key]["expires"] = service.clock() + 300
    assert service.manual_open_status()["used"] == 0
    next_day = await service.open_manual_write(dialog_key)
    assert next_day["quota"]["used"] == 1


async def test_failed_manual_telegram_open_does_not_consume_quota(service):
    dialog_key = "failed-token"
    service.dialog_tokens[dialog_key] = {
        "purpose": "manual_open", "peer_type": "user", "peer_id": "40",
        "access_hash": 4000, "display_title": "Person", "is_bot": False,
        "can_write": True, "source_id": None, "expires": service.clock() + 300,
    }
    service.client.get_messages = AsyncMock(side_effect=RuntimeError("telegram failed"))
    with pytest.raises(RuntimeError):
        await service.open_manual_write(dialog_key)
    assert service.manual_open_status()["used"] == 0
    assert service.store.library_source_for_peer("40", enabled_only=False) is None


@pytest.mark.parametrize("mention_only_mode", [True, False])
async def test_telegram_code_is_inbox_only_and_notification_is_redacted(
    service, settings, monkeypatch, mention_only_mode,
):
    from sqlalchemy import select
    from telethon.tl.types import InputPeerUser

    from app.db.tables import AlertJob, InboxNotification, MessageRecord
    from app.telegram.client import ingest_event
    from tests.fixtures.messages import msg
    from tests.test_mention_only import FakeEmail

    code_text = "Login code: 12345"
    monkeypatch.setattr(
        "app.telegram.client.event_to_stored_message",
        AsyncMock(return_value=msg(text=code_text)),
    )
    event = SimpleNamespace(
        chat_id=777000, sender_id=777000, out=False, raw_text=code_text, id=77,
        message=Message(77, code_text),
        get_sender=AsyncMock(return_value=SimpleNamespace(id=777000, bot=True)),
        get_chat=AsyncMock(return_value=SimpleNamespace(
            id=777000, first_name="Telegram", last_name="", forum=False,
        )),
        get_input_chat=AsyncMock(return_value=InputPeerUser(777000, 7000)),
    )
    email = FakeEmail()
    assert await ingest_event(
        event, settings=settings.model_copy(update={"mention_only_mode": mention_only_mode}),
        session_factory=service.store.factory, llm=None, email=email,
        ignored_chat_ids=set(), inbox=service, self_id=1,
    )
    notification = service.store.notifications(0)["events"][0]
    assert notification["title"] == "Telegram"
    assert notification["preview"] == "Новый код Telegram"
    assert notification["trigger_reason"] == "telegram_code"
    assert code_text not in str(notification)
    assert email.sent == []
    with service.store.factory() as session:
        assert list(session.scalars(select(MessageRecord))) == []
        assert list(session.scalars(select(AlertJob))) == []
        stored_notification = session.scalar(select(InboxNotification))
        assert code_text not in stored_notification.preview

    service.client.messages = [Message(77, code_text)]
    row = service.store.open(service.store.active()[0].id)
    history = await service.history(row.id)
    assert history["messages"][-1]["text"] == code_text


def test_concurrent_activation_keeps_one_canonical_row_and_all_events(service):
    from concurrent.futures import ThreadPoolExecutor

    def activate_one(message_id):
        return service.store.activate(
            peer_id="2", peer_type="user", thread_id=message_id, is_forum=False,
            title="Nikita", trigger_id=message_id, preview=str(message_id),
            reason="private_message",
        ).id

    with ThreadPoolExecutor(max_workers=8) as pool:
        ids = list(pool.map(activate_one, range(1, 21)))
    assert len(set(ids)) == 1
    row = service.store.get_any(ids[0])
    assert row.thread_id == 0
    assert row.latest_relevant_message_id == 20
    assert row.unread_count == 20
    assert len(service.store.notifications(0)["events"]) == 20


async def test_expired_projection_keeps_stable_id_for_later_message(service):
    row = service.store.activate(
        peer_id="2", peer_type="user", thread_id=0, is_forum=False,
        title="Nikita", trigger_id=1, preview="one", reason="private_message",
    )
    service.store.open(row.id)
    service.test_clock[0] += LIFETIME + 1
    await service.cleanup()
    assert service.store.active() == []
    assert service.store.get_any(row.id) is not None
    later = service.store.activate(
        peer_id="2", peer_type="user", thread_id=0, is_forum=False,
        title="Nikita", trigger_id=2, preview="two", reason="private_message",
    )
    assert later.id == row.id
    assert later.opened_at is None and later.unread_count == 1


def test_duplicate_migration_merges_private_rows_and_repoints_durable_refs(settings):
    from sqlalchemy import select

    from app.db.tables import InboxConversation, InboxNotification, InboxSend

    factory = init_db(settings)
    canonical_id, phantom_id = "a" * 32, "b" * 32
    with factory() as session:
        session.add_all([
            InboxConversation(
                id=canonical_id, peer_type="user", peer_id="2", access_hash="11",
                thread_id=0, is_forum=False, title="Nikita", topic_title="", preview="old",
                trigger_id=10, activated_at=1000, opened_at=1000,
                latest_relevant_message_id=10, last_seen_message_id=10, unread_count=0,
                expires_at=1300, manually_closed=False, library_source_id=None,
                quarantined_at=None, quarantine_reason=None,
            ),
            InboxConversation(
                id=phantom_id, peer_type="user", peer_id="2", access_hash="22",
                thread_id=77, is_forum=False, title="Nikita", topic_title="",
                preview="new", trigger_id=12, activated_at=1010, opened_at=None,
                latest_relevant_message_id=12, last_seen_message_id=0, unread_count=2,
                expires_at=0, manually_closed=False, library_source_id=None,
                quarantined_at=None, quarantine_reason=None,
            ),
            InboxNotification(
                peer_id="2", trigger_id=10, conversation_id=canonical_id, title="Nikita",
                topic_title="", preview="old", trigger_reason="private_message",
                unread_count=1, suppressed=False, created_at=1000,
            ),
            InboxNotification(
                peer_id="2", trigger_id=11, conversation_id=phantom_id, title="Nikita",
                topic_title="", preview="new", trigger_reason="private_message",
                unread_count=1, suppressed=False, created_at=1010,
            ),
            InboxNotification(
                peer_id="2", trigger_id=12, conversation_id=phantom_id, title="Nikita",
                topic_title="", preview="newer", trigger_reason="private_message",
                unread_count=2, suppressed=False, created_at=1020,
            ),
            InboxSend(
                request_id=str(uuid4()), conversation_id=phantom_id, status="sent",
                message_id=99, created_at=1010,
            ),
        ])
        session.commit()

    repeated = init_db(settings)
    init_db(settings)  # idempotent on the already-merged schema
    with repeated() as session:
        rows = list(session.scalars(select(InboxConversation)))
        assert len(rows) == 1
        row = rows[0]
        assert row.id == canonical_id and row.thread_id == 0
        assert row.trigger_id == 12 and row.latest_relevant_message_id == 12
        assert row.last_seen_message_id == 10 and row.unread_count == 2
        assert row.opened_at is None and row.preview == "new"
        notifications = list(session.scalars(select(InboxNotification).order_by(
            InboxNotification.id
        )))
        assert {item.conversation_id for item in notifications} == {canonical_id}
        assert [item.unread_count for item in notifications] == [0, 1, 2]
        assert session.scalar(select(InboxSend)).conversation_id == canonical_id


def test_handoff_zero_sentinel_read_state_is_repaired_once(settings):
    from sqlalchemy import select

    from app.db.tables import InboxConversation, InboxNotification

    factory = init_db(settings)
    opened_id, pending_id = "c" * 32, "d" * 32
    with factory() as session:
        session.add_all([
            InboxConversation(
                id=opened_id, peer_type="user", peer_id="10", access_hash=None,
                thread_id=0, is_forum=False, title="Opened", topic_title="",
                preview="seen", trigger_id=10, activated_at=1000, opened_at=1001,
                latest_relevant_message_id=0, last_seen_message_id=0, unread_count=0,
                expires_at=1301, manually_closed=False, library_source_id=None,
                quarantined_at=None, quarantine_reason=None,
            ),
            InboxConversation(
                id=pending_id, peer_type="user", peer_id="20", access_hash=None,
                thread_id=0, is_forum=False, title="Pending", topic_title="",
                preview="unseen", trigger_id=12, activated_at=1012, opened_at=None,
                latest_relevant_message_id=0, last_seen_message_id=0, unread_count=0,
                expires_at=0, manually_closed=False, library_source_id=None,
                quarantined_at=None, quarantine_reason=None,
            ),
            InboxNotification(
                peer_id="20", trigger_id=11, conversation_id=pending_id,
                title="Pending", topic_title="", preview="one",
                trigger_reason="private_message", unread_count=1,
                suppressed=False, created_at=1011,
            ),
            InboxNotification(
                peer_id="20", trigger_id=12, conversation_id=pending_id,
                title="Pending", topic_title="", preview="two",
                trigger_reason="private_message", unread_count=2,
                suppressed=False, created_at=1012,
            ),
        ])
        session.commit()

    upgraded = init_db(settings)
    init_db(settings)
    with upgraded() as session:
        opened = session.get(InboxConversation, opened_id)
        pending = session.get(InboxConversation, pending_id)
        assert (opened.latest_relevant_message_id, opened.last_seen_message_id,
                opened.unread_count) == (10, 10, 0)
        assert (pending.latest_relevant_message_id, pending.last_seen_message_id,
                pending.unread_count) == (12, 0, 2)
        assert len(list(session.scalars(select(InboxNotification).where(
            InboxNotification.conversation_id == pending_id
        )))) == 2


async def test_invalid_peer_is_quarantined_once_and_returns_gone(service):
    from telethon.errors import PeerIdInvalidError

    row = activate(service)
    service.client.get_messages = AsyncMock(side_effect=PeerIdInvalidError(request=None))
    with pytest.raises(InboxError) as first:
        await service.history(row.id)
    assert first.value.status == 410
    assert service.store.active() == []
    with pytest.raises(InboxError) as second:
        await service.history(row.id)
    assert second.value.status == 410
    assert service.client.get_messages.await_count == 1


async def test_peer_retry_failure_is_also_quarantined(service):
    from telethon.errors import PeerIdInvalidError
    from telethon.tl.types import InputPeerUser

    row = service.store.activate(
        peer_id="2", peer_type="user", access_hash=222, thread_id=0,
        is_forum=False, title="Nikita", trigger_id=10, preview="message",
        reason="private_message",
    )

    async def iter_dialogs(*, limit):
        assert limit == 200
        yield SimpleNamespace(
            id=2, name="Nikita", entity=SimpleNamespace(
                id=2, first_name="Nikita", last_name="", bot=False,
            ), input_entity=InputPeerUser(2, 333),
        )

    service.client.iter_dialogs = iter_dialogs
    service.client.get_messages = AsyncMock(side_effect=[
        PeerIdInvalidError(request=None), PeerIdInvalidError(request=None),
    ])
    with pytest.raises(InboxError) as failure:
        await service.history(row.id)
    assert failure.value.status == 410
    assert service.client.get_messages.await_count == 2
    assert service.store.get_any(row.id).quarantine_reason == "invalid_peer"


async def test_flood_wait_is_rate_limited_without_peer_refresh(service):
    from telethon.errors import FloodWaitError

    row = service.store.activate(
        peer_id="2", peer_type="user", access_hash=222, thread_id=0,
        is_forum=False, title="Nikita", trigger_id=10, preview="message",
        reason="private_message",
    )
    service.client.get_messages = AsyncMock(
        side_effect=FloodWaitError(request=None, capture=12)
    )
    with pytest.raises(InboxError) as failure:
        await service.history(row.id)
    assert failure.value.status == 429
    assert failure.value.retry_after == 12
    assert service.store.get(row.id) is not None
    with pytest.raises(InboxError) as cooldown:
        await service.history(row.id)
    assert cooldown.value.status == 429 and cooldown.value.retry_after == 12
    assert service.client.get_messages.await_count == 1
    service.test_clock[0] += 12
    with pytest.raises(InboxError):
        await service.history(row.id)
    assert service.client.get_messages.await_count == 2


async def test_saved_send_flood_wait_enters_shared_cooldown(service):
    from telethon.errors import FloodWaitError

    service.client.send_message.side_effect = FloodWaitError(request=None, capture=7)
    request_id = str(uuid4())
    with pytest.raises(InboxError) as failure:
        await service.send_saved(request_id, "remember")
    assert failure.value.status == 429 and failure.value.retry_after == 7
    with pytest.raises(InboxError) as cooldown:
        await service.send_saved(request_id, "remember")
    assert cooldown.value.status == 429 and cooldown.value.retry_after == 7
    assert service.client.send_message.await_count == 1


async def test_transient_rpc_during_peer_refresh_does_not_quarantine(service):
    from telethon.errors import PeerIdInvalidError, RPCError

    row = service.store.activate(
        peer_id="2", peer_type="user", access_hash=222, thread_id=0,
        is_forum=False, title="Nikita", trigger_id=10, preview="message",
        reason="private_message",
    )

    async def iter_dialogs(*, limit):
        assert limit == 200
        raise RPCError(request=None, message="temporary", code=500)
        yield  # pragma: no cover - keeps this an async generator

    service.client.iter_dialogs = iter_dialogs
    service.client.get_messages = AsyncMock(
        side_effect=PeerIdInvalidError(request=None)
    )
    with pytest.raises(InboxError) as failure:
        await service.history(row.id)
    assert failure.value.status == 503
    assert service.store.get(row.id) is not None
    assert service.store.get_any(row.id).quarantined_at is None


async def test_library_projection_cannot_send_through_inbox_routes(service, tmp_path):
    source = service.store.upsert_library_source(
        source_id="course", peer_type="chat", peer_id="-22", access_hash=None,
        display_title="Course", is_bot=False,
    )
    row = service.store.activate(
        peer_id="-22", peer_type="chat", thread_id=0, is_forum=False,
        title="Course", trigger_id=10, preview="lesson", reason="library_message",
        library_source_id=source.id,
    )
    upload = tmp_path / "notes.txt"
    upload.write_text("notes", encoding="utf-8")
    attempts = (
        service.send(row.id, str(uuid4()), "reply"),
        service.send(
            row.id, str(uuid4()), "upload", file_path=upload,
            filename="notes.txt", mime_type="text/plain",
        ),
    )
    for attempt in attempts:
        with pytest.raises(InboxError) as denied:
            await attempt
        assert denied.value.status == 403
    assert service.client.send_message.await_count == 0
    assert service.client.send_file.await_count == 0


async def test_selecting_existing_inbox_peer_immediately_disables_generic_send(service):
    row = service.store.activate(
        peer_id="-22", peer_type="chat", thread_id=0, is_forum=False,
        title="Course", trigger_id=10, preview="@fedocc question",
        reason="mention_only",
    )
    source = service.store.upsert_library_source(
        source_id="course", peer_type="chat", peer_id="-22", access_hash=None,
        display_title="Course", is_bot=False,
    )
    assert service.store.get(row.id).library_source_id == source.id
    with pytest.raises(InboxError) as denied:
        await service.send(row.id, str(uuid4()), "reply")
    assert denied.value.status == 403
    assert service.client.send_message.await_count == 0


def test_selecting_forum_source_closes_topic_rows_and_uses_one_projection(service):
    for thread in (7, 8):
        service.store.activate(
            peer_id="-1000000000022", peer_type="channel", thread_id=thread,
            is_forum=True, title="Course", trigger_id=thread, preview="mention",
            reason="mention_only",
        )
    source = service.store.upsert_library_source(
        source_id="course", peer_type="channel", peer_id="-1000000000022",
        access_hash=222, display_title="Course", is_bot=False,
    )
    assert service.store.active() == []
    projection = service.store.activate(
        peer_id="-1000000000022", peer_type="channel", thread_id=0,
        is_forum=False, title="Course", trigger_id=9, preview="lesson",
        reason="library_message", library_source_id=source.id,
    )
    assert [(row.id, row.thread_id) for row in service.store.active()] == [
        (projection.id, 0)
    ]


async def test_deselected_source_reclassifies_as_writable_ordinary_inbox(service):
    source = service.store.upsert_library_source(
        source_id="course", peer_type="chat", peer_id="-22", access_hash=None,
        display_title="Course", is_bot=False, notifications_muted=True,
    )
    projection = service.store.activate(
        peer_id="-22", peer_type="chat", thread_id=0, is_forum=False,
        title="Course", trigger_id=10, preview="library", reason="library_message",
        library_source_id=source.id, notifications_muted=True,
    )
    service.store.update_library_source(source.id, library_enabled=False)
    ordinary = service.store.activate(
        peer_id="-22", peer_type="chat", thread_id=0, is_forum=False,
        title="Course", trigger_id=11, preview="@fedocc check",
        reason="mention_only", library_source_id=None,
    )
    assert ordinary.id == projection.id
    assert ordinary.library_source_id is None and not ordinary.manually_closed
    assert service.store.notifications(1)["events"][0]["trigger_reason"] == "mention"
    sent = await service.send(ordinary.id, str(uuid4()), "reply")
    assert sent == {"message_id": 901}


async def test_exact_conversation_message_authorizes_its_media_download(service):
    row = activate(service)
    message = Message(777, "far away")
    message.file = SimpleNamespace(
        size=9, mime_type="application/pdf", name="lesson.pdf", duration=0,
    )
    service.client.messages = [message]
    exact = await service.conversation_message(row.id, message.id)
    assert exact["message"]["media"]["url"].endswith("/777")
    path, mime, name, inline = await service.media(row.id, message.id)
    assert path.read_bytes() == b"fake media"
    assert (mime, name, inline) == ("application/octet-stream", "lesson.pdf", False)


async def test_selected_muted_library_source_projects_without_banner(service):
    from telethon.tl.types import InputPeerChannel

    source = service.store.upsert_library_source(
        source_id="course", peer_type="channel", peer_id="-1000000000123",
        access_hash=777, display_title="Course", is_bot=False,
        notifications_muted=True,
    )
    event = SimpleNamespace(
        chat_id=-1000000000123, sender_id=55, out=False, raw_text="lesson", id=1,
        message=Message(1, "lesson"),
        get_chat=AsyncMock(return_value=SimpleNamespace(
            id=123, title="Course", forum=True, access_hash=777,
        )),
        get_input_chat=AsyncMock(return_value=InputPeerChannel(123, 777)),
    )
    await service.observe(event, trigger=None)
    row = service.store.active()[0]
    assert row.library_source_id == source.id and row.thread_id == 0
    assert row.unread_count == 1
    feed = service.store.notifications(0)
    assert feed["events"] == [] and feed["cursor"] == 1

    service.store.open(row.id)
    service.store.close(row.id)
    event.id = 2
    event.message = Message(2, "next")
    await service.observe(event, trigger=None)
    reopened = service.store.active()[0]
    assert reopened.id == row.id and reopened.unread_count == 1
    assert service.store.library_source(source.id).display_title == "Course"


def test_open_library_reports_only_its_opened_inbox_projection(service):
    source = service.store.upsert_library_source(
        source_id="course", peer_type="chat", peer_id="-22", access_hash=None,
        display_title="Course", is_bot=False,
    )
    projection = service.store.activate(
        peer_id="-22", peer_type="chat", thread_id=0, is_forum=False,
        title="Course", trigger_id=10, preview="lesson", reason="library_message",
        library_source_id=source.id,
    )
    unrelated = service.store.activate(
        peer_id="2", peer_type="user", thread_id=0, is_forum=False,
        title="Nikita", trigger_id=11, preview="hello", reason="private_message",
    )

    result = service.open_library(source.id)
    assert result["opened_conversation_ids"] == [projection.id]
    assert result["source"]["id"] == source.id
    opened = service.store.get(projection.id)
    assert opened.unread_count == 0 and opened.last_seen_message_id == 10
    assert service.store.get(unrelated.id).unread_count == 1

    expiry = opened.expires_at
    repeated = service.open_library(source.id)
    assert repeated["opened_conversation_ids"] == [projection.id]
    assert service.store.get(projection.id).expires_at == expiry


async def test_library_dialog_tokens_preferences_and_bot_write_are_validated(service):
    from telethon.tl.types import InputPeerUser

    bot = SimpleNamespace(id=42, first_name="Study Bot", last_name="", bot=True)
    human = SimpleNamespace(id=43, first_name="Human", last_name="", bot=False)
    dialogs = [
        SimpleNamespace(id=42, name="Study Bot", entity=bot,
                        input_entity=InputPeerUser(42, 4200)),
        SimpleNamespace(id=43, name="Human", entity=human,
                        input_entity=InputPeerUser(43, 4300)),
    ]

    async def iter_dialogs(*, limit):
        assert limit == 200
        for dialog in dialogs:
            yield dialog

    service.client.iter_dialogs = iter_dialogs
    payload = await service.library_dialogs()
    assert len(payload["dialogs"]) == 2
    assert all("peer_id" not in row and "peer_type" not in row and "access_hash" not in row
               for row in payload["dialogs"])
    bot_row = next(row for row in payload["dialogs"] if row["is_bot"])
    source = await service.update_library(bot_row["token"], {
        "library_enabled": True, "allow_bot_write": True,
        "notifications_muted": False, "digest_excluded": True,
    })
    assert source["writable"] is True
    result = await service.send_library(source["id"], str(uuid4()), "hello")
    assert result == {"message_id": 901}
    assert service.client.send_message.await_args.args[0] == 42

    human_row = next(row for row in payload["dialogs"] if not row["is_bot"])
    with pytest.raises(InboxError) as unsafe:
        await service.update_library(human_row["token"], {"allow_bot_write": True})
    assert unsafe.value.status == 422
    with pytest.raises(InboxError):
        await service.update_library("forged-token", {"library_enabled": True})


async def test_manual_picker_excludes_unwritable_ignored_self_and_telegram(service):
    from telethon.tl.types import InputPeerChannel, InputPeerChat, InputPeerUser

    service.test_ignored.add("44")
    entities = [
        (42, "Human", SimpleNamespace(id=42, bot=False), InputPeerUser(42, 4200)),
        (43, "Bot", SimpleNamespace(id=43, bot=True), InputPeerUser(43, 4300)),
        (1, "Self", SimpleNamespace(id=1, bot=False, is_self=True), InputPeerUser(1, 100)),
        (777000, "Telegram", SimpleNamespace(id=777000, bot=True),
         InputPeerUser(777000, 7770)),
        (44, "Ignored", SimpleNamespace(id=44, bot=False), InputPeerUser(44, 4400)),
        (-45, "Group", SimpleNamespace(id=45, left=False), InputPeerChat(45)),
        (-1000000000046, "Read only", SimpleNamespace(
            id=46, left=False, megagroup=False, gigagroup=False,
            creator=False, admin_rights=None,
        ), InputPeerChannel(46, 4600)),
        (-1000000000047, "Posting", SimpleNamespace(
            id=47, left=False, megagroup=False, gigagroup=False,
            creator=False, admin_rights=SimpleNamespace(post_messages=True),
        ), InputPeerChannel(47, 4700)),
        (48, "Deleted", SimpleNamespace(id=48, bot=False, deleted=True),
         InputPeerUser(48, 4800)),
    ]

    async def iter_dialogs(*, limit):
        assert limit == 200
        for dialog_id, name, entity, input_entity in entities:
            yield SimpleNamespace(
                id=dialog_id, name=name, entity=entity, input_entity=input_entity,
            )

    service.client.iter_dialogs = iter_dialogs
    payload = await service.manual_open_dialogs()
    assert {row["title"] for row in payload["dialogs"]} == {
        "Human", "Bot", "Group", "Posting",
    }
    assert payload["quota"] == service.manual_open_status()
    assert all(set(row) == {"token", "title", "is_bot"} for row in payload["dialogs"])


async def test_queued_library_disable_wins_before_later_bot_send(service):
    import asyncio

    source = service.store.upsert_library_source(
        source_id="studybot", peer_type="user", peer_id="42", access_hash=4200,
        display_title="Study Bot", is_bot=True, allow_bot_write=True,
    )
    await service.action_lock.acquire()
    disable = asyncio.create_task(service.update_library(source.id, {
        "library_enabled": False,
        "allow_bot_write": False,
    }))
    await asyncio.sleep(0)
    send = asyncio.create_task(service.send_library(
        source.id, str(uuid4()), "must not send",
    ))
    await asyncio.sleep(0)
    service.action_lock.release()

    disabled = await disable
    assert disabled["id"] == source.id
    with pytest.raises(InboxError) as denied:
        await send
    assert denied.value.status == 404
    assert service.client.send_message.await_count == 0


async def test_disabled_persisted_source_cannot_fall_back_to_static_access(service):
    from app.inbox.library import SAVED_MESSAGES, LibraryChat

    legacy = LibraryChat("n10022", -10022, "Legacy")
    service.library = (SAVED_MESSAGES, legacy)
    service.library_by_id = {row.id: row for row in service.library}
    persisted = service.store.upsert_library_source(
        source_id="persisted", peer_type="chat", peer_id="-10022",
        access_hash=None, display_title="Legacy", is_bot=False,
    )
    service.store.update_library_source(persisted.id, library_enabled=False)
    assert {row["id"] for row in service.library_json()} == {"saved"}

    reads_before = list(service.client.reads)
    for operation in (
        service.library_history(legacy.id),
        service.library_search(legacy.id, "lesson"),
        service.library_media(legacy.id, 1),
    ):
        with pytest.raises(InboxError) as disabled:
            await operation
        assert disabled.value.status == 404
    assert service.client.reads == reads_before


async def test_library_media_enforces_progress_limit_and_safe_inline_mime(service):
    from app.inbox.library import SAVED_MESSAGES, LibraryChat

    source = LibraryChat("n10022", -10022, "Course")
    service.library = (SAVED_MESSAGES, source)
    service.library_by_id = {row.id: row for row in service.library}
    message = Message(7, "file")
    message.file = SimpleNamespace(
        size=100, mime_type="image/svg+xml", name="active.svg", duration=0,
    )
    service.client.messages = [message]

    async def oversized(message, *, file, progress_callback):
        file.write(b"partial")
        progress_callback(message.file.size + 1, message.file.size + 1)

    service.client.download_media = oversized
    with pytest.raises(InboxError) as blocked:
        await service.library_media(source.id, message.id)
    assert blocked.value.status == 413
    assert not list(service.cache_dir.glob("library_*.part"))
    assert not list(service.cache_dir.glob("library_*.bin"))

    async def safe_download(message, *, file, progress_callback):
        progress_callback(message.file.size, message.file.size)
        file.write(b"safe")

    service.client.download_media = safe_download
    path, mime, _, inline = await service.library_media(source.id, message.id)
    assert path.read_bytes() == b"safe"
    assert mime == "application/octet-stream" and inline is False


async def test_library_media_flood_wait_cleans_partial_and_blocks_inbox(service):
    from telethon.errors import FloodWaitError

    from app.inbox.library import SAVED_MESSAGES, LibraryChat

    source = LibraryChat("n10022", -10022, "Course")
    service.library = (SAVED_MESSAGES, source)
    service.library_by_id = {row.id: row for row in service.library}
    message = Message(7, "file")
    message.file = SimpleNamespace(
        size=100, mime_type="application/pdf", name="lesson.pdf", duration=0,
    )
    service.client.messages = [message]
    row = activate(service, peer="-10099", trigger=99)
    calls = 0

    async def flood_wait(message, *, file, progress_callback):
        nonlocal calls
        calls += 1
        file.write(b"partial")
        raise FloodWaitError(request=None, capture=7)

    service.client.download_media = flood_wait
    with pytest.raises(InboxError) as failure:
        await service.library_media(source.id, message.id)
    assert failure.value.status == 429 and failure.value.retry_after == 7
    assert calls == 1
    assert not list(service.cache_dir.glob("library_*.part"))
    assert not list(service.cache_dir.glob("library_*.bin"))

    reads = list(service.client.reads)
    with pytest.raises(InboxError) as cooldown:
        await service.history(row.id)
    assert cooldown.value.status == 429 and cooldown.value.retry_after == 7
    assert service.client.reads == reads and calls == 1


async def test_library_three_pages_pins_exact_and_source_search(service):
    from app.inbox.library import SAVED_MESSAGES, LibraryChat

    source = LibraryChat("n10022", -10022, "Team")
    service.library = (SAVED_MESSAGES, source)
    service.library_by_id = {row.id: row for row in service.library}
    service.client.messages = [
        Message(mid, f"needle {mid}" if mid in {7, 107} else f"message {mid}",
                pinned=mid in {3, 140})
        for mid in range(1, 151)
    ]
    first = await service.library_history(source.id)
    second = await service.library_history(source.id, first["next_before"])
    third = await service.library_history(source.id, second["next_before"])
    assert [item["id"] for item in first["messages"]] == list(range(101, 151))
    assert [item["id"] for item in second["messages"]] == list(range(51, 101))
    assert [item["id"] for item in third["messages"]] == list(range(1, 51))
    assert third["next_before"] is None
    assert len({item["id"] for page in (first, second, third)
                for item in page["messages"]}) == 150

    pins = await service.library_pins(source.id)
    assert [item["id"] for item in pins["pins"]] == [3, 140]
    exact = await service.library_message(source.id, 3)
    assert exact["message"]["id"] == 3
    results = await service.library_search(source.id, "needle")
    assert [item["id"] for item in results["results"]] == [107, 7]


def test_telegram_link_segments_use_utf16_and_reject_malformed_urls():
    from telethon.tl.types import MessageEntityTextUrl, MessageEntityUrl

    from app.inbox.service import safe_link_segments

    text = "😀 кириллица лекция и https://example.com"
    label = "лекция"
    offset = len(text[:text.index(label)].encode("utf-16-le")) // 2
    url_start = text.index("https://")
    url_offset = len(text[:url_start].encode("utf-16-le")) // 2
    entities = [
        MessageEntityTextUrl(offset, len(label), "https://course.example/path"),
        MessageEntityUrl(url_offset, len("https://example.com")),
    ]
    segments = safe_link_segments(text, entities)
    assert "".join(item["text"] for item in segments) == text
    assert [item["url"] for item in segments if "url" in item] == [
        "https://course.example/path", "https://example.com",
    ]
    malformed = MessageEntityTextUrl(0, 1, "https://[broken")
    unsafe = MessageEntityTextUrl(0, 1, "javascript:alert(1)")
    assert safe_link_segments("x", [malformed, unsafe]) == []


def _digest_dialog(marked_id, input_entity, *, name, **entity_values):
    defaults = {"id": abs(marked_id), "bot": False, "is_self": False,
                "deactivated": False, "left": False, "megagroup": False,
                "gigagroup": False, "first_name": name, "last_name": ""}
    defaults.update(entity_values)
    return SimpleNamespace(
        id=marked_id, name=name, input_entity=input_entity,
        entity=SimpleNamespace(**defaults),
    )


class DigestTelegram(FakeTelegram):
    def __init__(self, dialogs, messages=None, failures=None, delay=0):
        super().__init__()
        self.dialogs = dialogs
        self.by_peer = messages or {}
        self.failures = failures or {}
        self.delay = delay
        self.fetch_peers = []
        self.active = 0
        self.max_active = 0

    async def iter_dialogs(self, *, limit):
        assert limit in {None, 200}
        for dialog in self.dialogs:
            yield dialog

    def _marked(self, peer):
        if isinstance(peer, int):
            return peer
        for dialog in self.dialogs:
            if peer == dialog.input_entity:
                return dialog.id
        return int(getattr(peer, "user_id", getattr(peer, "chat_id", 0)))

    async def get_messages(self, peer, **kwargs):
        marked = self._marked(peer)
        self.fetch_peers.append(marked)
        failure = self.failures.get(marked)
        if failure is not None:
            raise failure
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            messages = list(self.by_peer.get(marked, ()))
            if kwargs.get("offset_id"):
                messages = [item for item in messages if item.id < kwargs["offset_id"]]
            if kwargs.get("offset_date"):
                messages = [item for item in messages if item.date <= kwargs["offset_date"]]
            return sorted(messages, key=lambda item: item.id, reverse=True)[
                :kwargs.get("limit", 100)
            ]
        finally:
            self.active -= 1


async def test_digest_source_scope_filters_before_fetch_and_ignores_library_membership(
    settings, tmp_path,
):
    from telethon.tl.types import InputPeerChannel, InputPeerChat, InputPeerUser

    from app.inbox.digest import DigestItem, DigestPayload, latest_digest, preprocess, run_digest

    group = _digest_dialog(-10, InputPeerChat(10), name="Group")
    ignored = _digest_dialog(-11, InputPeerChat(11), name="Ignored")
    supergroup = _digest_dialog(
        -1000000000020, InputPeerChannel(20, 200), name="Forum", megagroup=True,
    )
    channel = _digest_dialog(
        -1000000000030, InputPeerChannel(30, 300), name="Announcements",
    )
    bot = _digest_dialog(2, InputPeerUser(2, 200), name="Utility Bot", bot=True)
    telegram = _digest_dialog(
        777000, InputPeerUser(777000, 777), name="Telegram", bot=True,
    )
    human = _digest_dialog(3, InputPeerUser(3, 300), name="Human")
    saved = _digest_dialog(1, InputPeerUser(1, 100), name="Saved", is_self=True)
    dialogs = [group, ignored, supergroup, channel, bot, telegram, human, saved]
    now = datetime(2026, 9, 21, 4, tzinfo=UTC)
    messages = {
        -10: [Message(10, "group news", date=now)],
        -1000000000020: [Message(20, "forum news", date=now)],
        -1000000000030: [Message(30, "channel news", date=now)],
        2: [Message(40, "bot news", date=now)],
        777000: [Message(41, "LOGIN CODE MARKER", date=now)],
        3: [Message(50, "PRIVATE HUMAN MARKER", date=now)],
        1: [Message(60, "SAVED MARKER", date=now)],
        -11: [Message(70, "IGNORED MARKER", date=now)],
    }
    client = DigestTelegram(dialogs, messages)
    service = InboxService(
        client, init_db(settings), lambda: {"-11"}, tmp_path / "digest-cache", self_id=1,
    )
    selected = service.store.upsert_library_source(
        source_id="disabled-group", peer_type="chat", peer_id="-10", access_hash=None,
        display_title="Group", is_bot=False, library_enabled=False,
    )

    rows = await service.digest_rows(now - timedelta(days=1), now)
    context, _ = preprocess(rows)
    text = str(context)
    assert {row["text"] for row in rows} == {
        "group news", "forum news", "channel news", "bot news",
    }
    assert selected.id in {row["source_id"] for row in rows}
    assert "PRIVATE HUMAN MARKER" not in text
    assert "SAVED MARKER" not in text and "IGNORED MARKER" not in text
    assert all(peer not in client.fetch_peers for peer in (3, 1, -11, 777000))
    assert service.last_digest_report == {
        "total_dialogs": 8, "included_groups": 2, "included_channels": 1,
        "included_bots": 1, "excluded_human_dms": 1, "ignored": 1,
        "unavailable": 0, "redactions": 0,
    }

    class CaptureProvider:
        calls = []

        def generate(self, supplied, *, candidate_mode=False):
            self.calls.append((supplied, candidate_mode))
            return DigestPayload(title="Scope", items=[DigestItem(
                category="news", title="News", summary="Safe",
                source_refs=[supplied[0]["ref"]],
            )])

    provider = CaptureProvider()
    await run_digest(service, service.store.factory, settings, now, provider)
    assert "PRIVATE HUMAN MARKER" not in str(provider.calls)
    link = latest_digest(service.store.factory)["items"][0]["links"][0]
    assert link.startswith("/?library=") and "-100" not in link


async def test_digest_permanent_source_failure_isolated_and_library_soft_disabled(
    settings, tmp_path,
):
    from telethon.errors import PeerIdInvalidError
    from telethon.tl.types import InputPeerChat

    dead = _digest_dialog(-20, InputPeerChat(20), name="Dead group")
    live = _digest_dialog(-21, InputPeerChat(21), name="Live group")
    now = datetime(2026, 9, 21, 4, tzinfo=UTC)
    client = DigestTelegram(
        [dead, live], {-21: [Message(1, "live news", date=now)]},
        failures={-20: PeerIdInvalidError(request=None)},
    )
    service = InboxService(
        client, init_db(settings), lambda: set(), tmp_path / "digest-cache", self_id=1,
    )
    source = service.store.upsert_library_source(
        source_id="dead", peer_type="chat", peer_id="-20", access_hash=None,
        display_title="Dead group", is_bot=False,
    )
    service.library_snapshots[(source.id, 0)] = {"payload": {}, "fetched": 0}

    rows = await service.digest_rows(now - timedelta(days=1), now)
    assert [row["text"] for row in rows] == ["live news"]
    assert service.store.library_source(source.id, enabled_only=False).library_enabled is False
    assert not service.library_snapshots
    assert service.last_digest_report["unavailable"] == 1


async def test_digest_transient_failure_stays_retryable_and_does_not_disable(
    settings, tmp_path,
):
    from telethon.errors import FloodWaitError
    from telethon.tl.types import InputPeerChat

    dialog = _digest_dialog(-30, InputPeerChat(30), name="Temporary")
    client = DigestTelegram(
        [dialog], failures={-30: FloodWaitError(request=None, capture=9)},
    )
    service = InboxService(
        client, init_db(settings), lambda: set(), tmp_path / "digest-cache", self_id=1,
    )
    source = service.store.upsert_library_source(
        source_id="temporary", peer_type="chat", peer_id="-30", access_hash=None,
        display_title="Temporary", is_bot=False,
    )
    now = datetime(2026, 9, 21, 4, tzinfo=UTC)
    with pytest.raises(InboxError) as failure:
        await service.digest_rows(now - timedelta(days=1), now)
    assert failure.value.status == 429
    assert service.store.library_source(source.id, enabled_only=False).library_enabled is True


async def test_digest_fetch_uses_bounded_concurrency(settings, tmp_path):
    from telethon.tl.types import InputPeerChat

    dialogs = [
        _digest_dialog(-index, InputPeerChat(index), name=f"Group {index}")
        for index in range(1, 9)
    ]
    client = DigestTelegram(dialogs, delay=0.01)
    service = InboxService(
        client, init_db(settings), lambda: set(), tmp_path / "digest-cache", self_id=99,
    )
    now = datetime(2026, 9, 21, 4, tzinfo=UTC)
    await service.digest_rows(now - timedelta(days=1), now)
    assert 1 < client.max_active <= 4


async def test_digest_daily_reconciliation_disables_only_confirmed_missing_library_source(
    settings, tmp_path,
):
    from telethon.errors import PeerIdInvalidError

    client = DigestTelegram([], failures={-40: PeerIdInvalidError(request=None)})
    service = InboxService(
        client, init_db(settings), lambda: set(), tmp_path / "digest-cache", self_id=1,
    )
    source = service.store.upsert_library_source(
        source_id="missing", peer_type="chat", peer_id="-40", access_hash=None,
        display_title="Missing", is_bot=False,
    )
    now = datetime(2026, 9, 21, 4, tzinfo=UTC)
    assert await service.digest_rows(now - timedelta(days=1), now) == []
    assert service.store.library_source(source.id, enabled_only=False).library_enabled is False
    assert service.last_digest_report["unavailable"] == 1


async def test_digest_503_does_not_disable_library_source(settings, tmp_path):
    from telethon.tl.types import InputPeerChat

    dialog = _digest_dialog(-50, InputPeerChat(50), name="Transient")
    service = InboxService(
        DigestTelegram([dialog]), init_db(settings), lambda: set(),
        tmp_path / "digest-cache", self_id=1,
    )
    source = service.store.upsert_library_source(
        source_id="transient", peer_type="chat", peer_id="-50", access_hash=None,
        display_title="Transient", is_bot=False,
    )
    service._peer_call = AsyncMock(side_effect=InboxError("temporary", 503))
    now = datetime(2026, 9, 21, 4, tzinfo=UTC)
    with pytest.raises(InboxError) as failure:
        await service.digest_rows(now - timedelta(days=1), now)
    assert failure.value.status == 503
    assert service.store.library_source(source.id, enabled_only=False).library_enabled is True
