from __future__ import annotations

import asyncio
import io
import math
import secrets
import time
import warnings
from collections import OrderedDict
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlsplit

from PIL import Image, ImageOps, UnidentifiedImageError
from sqlalchemy import delete
from telethon import utils
from telethon.errors import (
    ChannelInvalidError,
    ChannelPrivateError,
    ChannelPublicGroupNaError,
    ChatIdInvalidError,
    FloodWaitError,
    PeerIdInvalidError,
    RPCError,
    UserIdInvalidError,
)
from telethon.tl import types as tl_types
from telethon.tl.functions.messages import GetCustomEmojiDocumentsRequest
from telethon.tl.types import (
    Channel,
    Chat,
    InputMessagesFilterPinned,
    InputPeerChannel,
    InputPeerChat,
    InputPeerUser,
    MessageEntityCustomEmoji,
    MessageEntityTextUrl,
    MessageEntityUrl,
    PeerChannel,
    User,
)

from app.db.tables import InboxSend
from app.inbox.library import SAVED_MESSAGES, LibraryChat, peer_type_for_marked_id
from app.inbox.push import WebPushService
from app.inbox.store import InboxStore
from app.services.attention import INBOX_TRIGGER_TYPES, classify_incoming
from app.services.mentions import has_exact_fedocc_mention
from app.telegram.mapper import display_name

MAX_IMAGE = 10 * 1024 * 1024
MAX_MEDIA = 64 * 1024 * 1024
MAX_CACHE = 256 * 1024 * 1024
INLINE_MEDIA_TYPES = frozenset({
    "image/jpeg", "image/png", "image/webp", "video/mp4", "video/webm",
    "audio/ogg", "audio/mpeg", "audio/mp4", "audio/wav",
})
LIBRARY_PAGE_SIZE = 50
SEARCH_PAGE_SIZE = 30
PIN_CACHE_SECONDS = 60
MEDIA_ALLOW_SECONDS = 120
PEER_RETRY_SECONDS = 60
PEER_ERRORS = (
    PeerIdInvalidError,
    ChannelInvalidError,
    ChannelPrivateError,
    ChannelPublicGroupNaError,
    ChatIdInvalidError,
    UserIdInvalidError,
)
RESOLUTION_ERRORS = (ValueError, *PEER_ERRORS)


class InboxError(Exception):
    def __init__(self, message, status=400, *, retry_after=None):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


class PeerRefreshUnavailable(Exception):
    """A transient Telegram failure, distinct from a completed no-match scan."""


def _service_person(entity) -> str:
    if entity is None:
        return ""
    return " ".join(display_name(entity).split())[:160]


def _service_title(value) -> str:
    return " ".join(str(value or "").split())[:300]


def service_message_text(message) -> str | None:
    """Render Telegram service actions using only entities resolved with the message batch."""
    action = getattr(message, "action", None)
    if action is None:
        return None
    actor = _service_person(getattr(message, "sender", None))
    entities = [entity for entity in (getattr(message, "_action_entities", ()) or ())
                if entity is not None]
    people = [_service_person(entity) for entity in entities]
    people = [name for name in people if name]

    if isinstance(action, tl_types.MessageActionChatAddUser):
        ids = list(getattr(action, "users", ()) or ())
        if len(ids) == 1 and ids[0] == getattr(message, "sender_id", None):
            return f"{actor or (people[0] if people else 'Участник')} присоединился к группе"
        if people:
            noun = "участника" if len(people) == 1 else "участников"
            if actor:
                return f"{actor} добавил {noun}: {', '.join(people)}"
            return (f"Добавлен участник: {people[0]}" if len(people) == 1
                    else f"Добавлены участники: {', '.join(people)}")
        return f"{actor} добавил участника" if actor else "Добавлен участник"
    if isinstance(action, tl_types.MessageActionChatJoinedByLink):
        return f"{actor or 'Участник'} присоединился по ссылке"
    if isinstance(action, tl_types.MessageActionChatJoinedByRequest):
        return f"{actor or 'Участник'} присоединился по запросу"
    if isinstance(action, tl_types.MessageActionChatDeleteUser):
        target = people[0] if people else ""
        if getattr(action, "user_id", None) == getattr(message, "sender_id", None):
            return f"{target or actor or 'Участник'} покинул группу"
        if actor and target:
            return f"{actor} удалил участника: {target}"
        if target:
            return f"Участник удалён: {target}"
        return f"{actor} удалил участника" if actor else "Участник покинул группу"
    if isinstance(action, tl_types.MessageActionChatCreate):
        title = _service_title(action.title)
        return f"{actor or 'Создатель'} создал группу «{title}»" if title else "Группа создана"
    if isinstance(action, tl_types.MessageActionChannelCreate):
        title = _service_title(action.title)
        return f"{actor or 'Создатель'} создал канал «{title}»" if title else "Канал создан"
    if isinstance(action, tl_types.MessageActionChatEditTitle):
        title = _service_title(action.title)
        return (f"{actor} изменил название на «{title}»" if actor and title
                else f"Название группы изменено на «{title}»" if title
                else "Название группы изменено")
    if isinstance(action, tl_types.MessageActionChatEditPhoto):
        return f"{actor} изменил фото группы" if actor else "Фото группы изменено"
    if isinstance(action, tl_types.MessageActionChatDeletePhoto):
        return f"{actor} удалил фото группы" if actor else "Фото группы удалено"
    if isinstance(action, tl_types.MessageActionPinMessage):
        return f"{actor} закрепил сообщение" if actor else "Сообщение закреплено"
    if isinstance(action, tl_types.MessageActionChatMigrateTo):
        return "Группа преобразована в супергруппу"
    if isinstance(action, tl_types.MessageActionChannelMigrateFrom):
        return "История группы перенесена в супергруппу"
    if isinstance(action, tl_types.MessageActionTopicCreate):
        title = _service_title(action.title)
        return f"Создана тема «{title}»" if title else "Создана тема"
    if isinstance(action, tl_types.MessageActionTopicEdit):
        title = _service_title(getattr(action, "title", None))
        return f"Тема переименована в «{title}»" if title else "Настройки темы изменены"
    if isinstance(action, tl_types.MessageActionCustomAction):
        return _service_title(action.message) or "Системное событие"
    return "Системное событие"


def forum_thread(message):
    header = getattr(message, "reply_to", None)
    if header and getattr(header, "forum_topic", False):
        return header.reply_to_top_id or header.reply_to_msg_id or 1
    return 1


async def thread_context(message, chat, *, peer_type=None):
    # Telegram reply metadata is not a conversation identity in private dialogs.
    # Treating reply_to_top_id as a thread there causes a phantom projection and
    # makes Telethon issue messages.GetReplies against an InputPeerUser.
    if peer_type == "user" or isinstance(chat, User):
        return 0, False
    if getattr(chat, "forum", False):
        return forum_thread(message), True
    header = getattr(message, "reply_to", None)
    if header and getattr(header, "reply_to_top_id", None):
        return header.reply_to_top_id, False
    # Ordinary group replies remain peer-wide. Discussion replies are rooted in
    # the automatically forwarded channel post in the discussion group.
    parent = message
    for _ in range(12):
        forwarded = getattr(parent, "fwd_from", None)
        if forwarded and isinstance(getattr(forwarded, "from_id", None), PeerChannel):
            if getattr(forwarded, "channel_post", None):
                return parent.id, False
        if not getattr(parent, "reply_to_msg_id", None):
            break
        parent = await parent.get_reply_message()
        if parent is None:
            break
    return 0, False


def normalize_image(raw):
    if not raw or len(raw) > MAX_IMAGE:
        raise InboxError("Изображение должно быть не больше 10 МБ.")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw), formats=["JPEG", "PNG", "WEBP"]) as picture:
                if picture.format not in {"JPEG", "PNG", "WEBP"}:
                    raise InboxError("Можно отправлять только JPEG, PNG и WebP.")
                if getattr(picture, "n_frames", 1) != 1:
                    raise InboxError("Анимированные изображения не поддерживаются.")
                if picture.width * picture.height > 20_000_000:
                    raise InboxError("Изображение слишком большое: максимум 20 мегапикселей.")
                picture.load()
                picture = ImageOps.exif_transpose(picture)
                picture.thumbnail((2560, 2560))
                rgba = picture.convert("RGBA")
                background = Image.new("RGB", picture.size, "white")
                background.paste(rgba, mask=rgba.getchannel("A"))
                output = io.BytesIO()
                background.save(output, format="JPEG", quality=90)
                output.seek(0)
                output.name = "image.jpg"
                return output
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError,
            Image.DecompressionBombWarning):
        raise InboxError("Не удалось прочитать изображение.") from None


def conversation_json(row):
    return {name: getattr(row, name) for name in (
        "id", "title", "topic_title", "preview", "trigger_id", "activated_at", "opened_at",
        "latest_relevant_message_id", "last_seen_message_id", "unread_count",
    )} | {"thread_id": row.thread_id,
          "expires_at": row.expires_at if row.opened_at is not None else None}


def rich_text_segments(text, entities, custom_emoji=None):
    """Split Telegram UTF-16 entities without disturbing overlapping entity offsets."""
    if not text:
        return []
    encoded = text.encode("utf-16-le")
    spans = []
    for entity in entities or ():
        if not isinstance(entity, (MessageEntityTextUrl, MessageEntityUrl,
                                   MessageEntityCustomEmoji)):
            continue
        start, end = entity.offset * 2, (entity.offset + entity.length) * 2
        if start < 0 or end > len(encoded) or start >= end:
            continue
        try:
            label = encoded[start:end].decode("utf-16-le")
        except UnicodeDecodeError:
            continue
        if isinstance(entity, MessageEntityCustomEmoji):
            document_id = int(entity.document_id)
            detail = (custom_emoji or {}).get(document_id)
            spans.append((start, end, {"text": label, "custom_emoji": detail or {
                "document_id": str(document_id), "available": False,
            }}))
            continue
        url = entity.url if isinstance(entity, MessageEntityTextUrl) else label
        if not isinstance(url, str):
            continue
        try:
            parsed = urlsplit(url)
        except ValueError:
            continue
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
            continue
        spans.append((start, end, {"text": label, "url": url}))
    if not spans:
        return []
    boundaries = {0, len(encoded)}
    for start, end, _ in spans:
        boundaries.update((start, end))
    points = sorted(boundaries)
    segments = []
    for start, end in zip(points, points[1:], strict=False):
        try:
            label = encoded[start:end].decode("utf-16-le")
        except UnicodeDecodeError:
            return []
        segment = {"text": label}
        covering = [value for left, right, value in spans if left <= start and end <= right]
        emoji = next((value.get("custom_emoji") for value in covering
                      if value.get("custom_emoji") is not None), None)
        link = next((value.get("url") for value in covering if value.get("url")), None)
        if emoji is not None:
            segment["custom_emoji"] = emoji
        if link is not None:
            segment["url"] = link
        segments.append(segment)
    return segments


def safe_link_segments(text, entities):
    """Compatibility wrapper used by callers and tests."""
    return rich_text_segments(text, entities)


class InboxService:
    def __init__(self, client, factory, ignored, cache_dir, self_id, clock=time.time,
                 library=None, upload_max_mb=100, upload_concurrency=2,
                 upload_stale_hours=24, web_push_private_key="", web_push_subject=""):
        self.client = client
        self.store = InboxStore(factory, ignored, clock)
        self.store.clamp_existing_lifetimes()
        self.clock = clock
        self.self_id = self_id
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.snapshots = OrderedDict()
        self.history_lock = asyncio.Lock()
        self.action_lock = asyncio.Lock()
        self.media_lock = asyncio.Lock()
        self.library = tuple(library or (SAVED_MESSAGES,))
        self.library_by_id = {row.id: row for row in self.library}
        self.store.seed_library(self.library)
        self.library_snapshots = OrderedDict()
        self.pin_snapshots = OrderedDict()
        self.media_allowances = OrderedDict()
        self.dialog_tokens = OrderedDict()
        self.peer_failures = {}
        self.custom_emoji_documents = {}
        self.custom_emoji_failures = {}
        self.telegram_blocked_until = 0.0
        self.upload_max = upload_max_mb * 1024 * 1024
        self.upload_stale = upload_stale_hours * 3600
        self.upload_dir = self.cache_dir.parent / "outbox_tmp"
        self.upload_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.upload_slots = asyncio.Semaphore(upload_concurrency)
        self.push = WebPushService(
            factory, self.store, web_push_private_key, web_push_subject, clock=clock,
        )

    def require(self, key):
        row = self.store.get(key)
        if row is None:
            stored = self.store.get_any(key)
            if stored is not None and stored.quarantined_at is not None:
                raise InboxError("Разговор больше недоступен в Telegram.", 410)
            raise InboxError("Разговор закрыт или время активности истекло.", 404)
        return row

    @staticmethod
    def _input_identity(input_peer, marked_id, chat=None):
        access_hash = getattr(input_peer, "access_hash", None)
        if isinstance(input_peer, InputPeerUser) or isinstance(chat, User):
            peer_type = "user"
        elif isinstance(input_peer, InputPeerChannel) or isinstance(chat, Channel):
            peer_type = "channel"
        elif isinstance(input_peer, InputPeerChat) or isinstance(chat, Chat):
            peer_type = "chat"
        else:
            peer_type = peer_type_for_marked_id(marked_id)
            access_hash = access_hash if access_hash is not None else getattr(
                chat, "access_hash", None
            )
        return peer_type, str(int(marked_id)), access_hash

    async def _event_identity(self, event, chat):
        input_peer = None
        getter = getattr(event, "get_input_chat", None)
        if getter is not None:
            try:
                input_peer = await getter()
            except (RPCError, ValueError):
                input_peer = None
        return self._input_identity(input_peer, event.chat_id, chat)

    async def observe(self, event, *, trigger=None, sender_is_bot=False):
        if (str(event.chat_id) in self.store.ignored() or event.out
                or event.sender_id == self.self_id):
            return
        source = self.store.library_source_for_peer(event.chat_id)
        # Bots remain excluded from ordinary Inbox/email, but an explicitly
        # selected Library bot still gets its one attention projection.
        if sender_is_bot and source is None:
            return
        if trigger is None and source is None:
            trigger = await classify_incoming(event.message, self_id=self.self_id,
                                               text=event.raw_text, sender_id=event.sender_id)
        if source is None and trigger not in INBOX_TRIGGER_TYPES:
            return
        chat = await event.get_chat()
        peer_type, peer_id, access_hash = await self._event_identity(event, chat)
        if source is not None:
            # A selected source has one peer-wide attention projection even when
            # the source itself is a forum with many topics.
            thread, forum = 0, False
            reason = trigger if trigger in INBOX_TRIGGER_TYPES else "library_message"
            title = source.display_title or display_name(chat)
        else:
            thread, forum = await thread_context(
                event.message, chat, peer_type=peer_type
            )
            reason = trigger
            title = display_name(chat)
        self.store.activate(
            peer_id=peer_id,
            peer_type=peer_type,
            access_hash=access_hash,
            thread_id=thread,
            is_forum=forum,
            title=title,
            trigger_id=event.id,
            preview=event.raw_text or "",
            reason=reason,
            library_source_id=source.id if source else None,
            notifications_muted=bool(source and source.notifications_muted),
        )

    @staticmethod
    def _source_json(source):
        if isinstance(source, LibraryChat):
            return {
                "id": source.id,
                "title": source.title,
                "writable": source.writable,
                "notifications_muted": False,
                "allow_bot_write": False,
                "digest_excluded": False,
                "sort_order": 0,
                "is_bot": False,
            }
        return {
            "id": source.id,
            "title": source.display_title,
            "writable": bool(source.is_bot and source.allow_bot_write),
            "notifications_muted": bool(source.notifications_muted),
            "allow_bot_write": bool(source.allow_bot_write),
            "digest_excluded": bool(source.digest_excluded),
            "sort_order": int(source.sort_order),
            "is_bot": bool(source.is_bot),
        }

    def library_json(self):
        persisted = self.store.library_sources(enabled_only=False)
        sources = [SAVED_MESSAGES, *(row for row in persisted if row.library_enabled)]
        known_ids = {SAVED_MESSAGES.id, *(row.id for row in persisted)}
        known_peers = {(row.peer_type, str(row.peer_id)) for row in persisted}
        # Backward compatibility for callers that replace ``service.library``
        # directly in tests or small integrations.  A persisted disabled source
        # is authoritative and must never be resurrected by the old static list.
        sources.extend(
            row for row in self.library
            if row.id not in known_ids
            and (
                peer_type_for_marked_id(row.peer_id), str(row.peer_id)
            ) not in known_peers
        )
        return [self._source_json(row) for row in sources]

    def library_source(self, source_id):
        if source_id == SAVED_MESSAGES.id:
            return SAVED_MESSAGES
        persisted = self.store.library_source(source_id, enabled_only=False)
        if persisted is not None:
            if not persisted.library_enabled:
                raise InboxError("Раздел библиотеки не найден.", 404)
            return persisted
        source = self.library_by_id.get(source_id)
        if source is not None and self.store.library_source_for_peer(
            source.peer_id, enabled_only=False
        ) is not None:
            # The DB preference (including a disabled preference) wins over the
            # compatibility-only static object even if their opaque IDs differ.
            source = None
        if source is None:
            raise InboxError("Раздел библиотеки не найден.", 404)
        return source

    @staticmethod
    def _source_title(source):
        return source.title if isinstance(source, LibraryChat) else source.display_title

    @staticmethod
    def _source_writable(source):
        if isinstance(source, LibraryChat):
            return source.writable
        return bool(source.is_bot and source.allow_bot_write)

    def _flood_wait_error(self, exc=None):
        if exc is not None:
            seconds = max(1, int(getattr(exc, "seconds", 1) or 1))
            self.telegram_blocked_until = max(
                self.telegram_blocked_until, self.clock() + seconds
            )
        remaining = max(1, math.ceil(self.telegram_blocked_until - self.clock()))
        return InboxError(
            "Telegram временно ограничил запрос. Повторите позже.",
            429,
            retry_after=remaining,
        )

    def _check_telegram_cooldown(self):
        if self.clock() < self.telegram_blocked_until:
            raise self._flood_wait_error()

    async def _refresh_peer(self, peer_type, peer_id):
        failure_key = (peer_type, str(peer_id))
        failed_at = self.peer_failures.get(failure_key)
        if failed_at is not None and self.clock() - failed_at < PEER_RETRY_SECONDS:
            raise ValueError("peer resolution is temporarily quarantined")
        iterator = getattr(self.client, "iter_dialogs", None)
        if iterator is None:
            self.peer_failures[failure_key] = self.clock()
            raise ValueError("dialog resolution is unavailable")
        try:
            async for dialog in iterator(limit=200):
                if str(int(dialog.id)) != str(int(peer_id)):
                    continue
                input_peer = getattr(dialog, "input_entity", None)
                entity = getattr(dialog, "entity", None)
                resolved_type, marked, access_hash = self._input_identity(
                    input_peer, dialog.id, entity
                )
                self.store.remember_peer(
                    resolved_type,
                    marked,
                    access_hash,
                    title=getattr(dialog, "name", None) or display_name(entity),
                    is_bot=bool(getattr(entity, "bot", False)),
                )
                self.peer_failures.pop(failure_key, None)
                return input_peer or int(marked)
        except FloodWaitError:
            raise
        except RPCError as exc:
            raise PeerRefreshUnavailable("dialog resolution failed") from exc
        self.peer_failures[failure_key] = self.clock()
        raise ValueError("peer is no longer present in dialogs")

    async def _resolve_peer(self, target, *, force=False):
        if isinstance(target, LibraryChat) and target.peer_id == "me":
            return "me"
        peer_type = getattr(target, "peer_type", None) or peer_type_for_marked_id(
            target.peer_id
        )
        peer_id = str(int(target.peer_id))
        access_hash = getattr(target, "access_hash", None)
        if force:
            return await self._refresh_peer(peer_type, peer_id)
        resolver = getattr(self.client, "get_input_entity", None)
        if resolver is None:
            # Small fake clients and legacy adapters accept marked integer IDs.
            return int(peer_id)
        raw_id, _ = utils.resolve_id(int(peer_id))
        if peer_type == "chat":
            return InputPeerChat(raw_id)
        if access_hash is not None:
            if peer_type == "user":
                return InputPeerUser(raw_id, int(access_hash))
            if peer_type == "channel":
                return InputPeerChannel(raw_id, int(access_hash))
        try:
            return await resolver(int(peer_id))
        except RESOLUTION_ERRORS:
            return await self._refresh_peer(peer_type, peer_id)

    async def _peer_call(self, target, operation, *, quarantine_key=None):
        def gone():
            if quarantine_key:
                self.store.quarantine(quarantine_key, "invalid_peer")
                self.snapshots.pop(quarantine_key, None)
            return InboxError("Источник больше недоступен в Telegram.", 410)

        self._check_telegram_cooldown()
        try:
            peer = await self._resolve_peer(target)
        except FloodWaitError as exc:
            raise self._flood_wait_error(exc) from None
        except PeerRefreshUnavailable:
            raise InboxError("Telegram временно недоступен. Повторите позже.", 503) from None
        except RESOLUTION_ERRORS:
            try:
                peer = await self._resolve_peer(target, force=True)
            except FloodWaitError as exc:
                raise self._flood_wait_error(exc) from None
            except PeerRefreshUnavailable:
                raise InboxError(
                    "Telegram временно недоступен. Повторите позже.", 503
                ) from None
            except RESOLUTION_ERRORS:
                raise gone() from None
        try:
            return await operation(peer)
        except FloodWaitError as exc:
            raise self._flood_wait_error(exc) from None
        except PEER_ERRORS:
            try:
                peer = await self._resolve_peer(target, force=True)
            except FloodWaitError as exc:
                raise self._flood_wait_error(exc) from None
            except PeerRefreshUnavailable:
                raise InboxError(
                    "Telegram временно недоступен. Повторите позже.", 503
                ) from None
            except RESOLUTION_ERRORS:
                raise gone() from None
            try:
                return await operation(peer)
            except FloodWaitError as exc:
                raise self._flood_wait_error(exc) from None
            except PEER_ERRORS:
                raise gone() from None

    async def serialize_library(self, message, source_id, by_id):
        row = type("LibraryRow", (), {"id": source_id, "is_forum": False,
                                      "thread_id": 0})()
        result = await self.serialize(message, row, by_id)
        if result and result["media"]:
            result["media"]["url"] = f"/api/library/{source_id}/media/{message.id}"
        return result

    async def serialize_many(self, messages, row, by_id, *, source_id=None):
        await self._resolve_custom_emoji(messages)
        result = []
        for message in messages:
            item = (await self.serialize_library(message, source_id, by_id)
                    if source_id is not None else await self.serialize(message, row, by_id))
            if item is not None:
                result.append(item)
        return result

    async def _resolve_custom_emoji(self, messages):
        ids = {int(entity.document_id) for message in messages
               for entity in (getattr(message, "entities", None) or ())
               if isinstance(entity, MessageEntityCustomEmoji)}
        ids.update(
            int(reaction.document_id)
            for message in messages
            for result in (getattr(getattr(message, "reactions", None), "results", None) or ())
            if isinstance((reaction := getattr(result, "reaction", None)),
                          tl_types.ReactionCustomEmoji)
        )
        now = self.clock()
        unknown = sorted(value for value in ids
                         if value not in self.custom_emoji_documents
                         and self.custom_emoji_failures.get(value, 0) <= now)
        if not unknown:
            return
        try:
            documents = await self.client(GetCustomEmojiDocumentsRequest(unknown))
        except (RPCError, ValueError, TypeError):
            for value in unknown:
                self.custom_emoji_failures[value] = now + 300
            return
        found = {int(document.id): document for document in documents}
        self.custom_emoji_documents.update(found)
        for value in unknown:
            if value not in found:
                self.custom_emoji_failures[value] = now + 300

    def _custom_emoji_payloads(self, entities):
        result = {}
        for entity in entities or ():
            if not isinstance(entity, MessageEntityCustomEmoji):
                continue
            document_id = int(entity.document_id)
            document = self.custom_emoji_documents.get(document_id)
            if document is None:
                continue
            mime = (getattr(document, "mime_type", "") or "").lower()
            if mime.startswith("image/"):
                kind = "static"
            elif mime == "video/webm":
                kind = "video"
            elif mime == "application/x-tgsticker":
                kind = "animated"
            else:
                kind = "unsupported"
            attribute = next((item for item in getattr(document, "attributes", ())
                              if isinstance(item, tl_types.DocumentAttributeCustomEmoji)), None)
            size = int(getattr(document, "size", 0) or 0)
            result[document_id] = {
                "document_id": str(document_id), "format": kind,
                "available": bool(size and size <= MAX_MEDIA and kind != "unsupported"),
                "text_color": bool(getattr(attribute, "text_color", False)),
                "url": f"/api/custom-emoji/{document_id}",
            }
        return result

    def _reaction_payloads(self, message):
        output = []
        results = getattr(getattr(message, "reactions", None), "results", None) or ()
        for result in results:
            count = int(getattr(result, "count", 0) or 0)
            if count <= 0:
                continue
            reaction = getattr(result, "reaction", None)
            if isinstance(reaction, tl_types.ReactionEmoji):
                output.append({"type": "emoji", "emoji": reaction.emoticon, "count": count})
            elif isinstance(reaction, tl_types.ReactionCustomEmoji):
                document_id = int(reaction.document_id)
                custom = self._custom_emoji_payloads((
                    MessageEntityCustomEmoji(0, 1, document_id),
                )).get(document_id, {
                    "document_id": str(document_id), "format": "unsupported",
                    "available": False, "text_color": False,
                    "url": f"/api/custom-emoji/{document_id}",
                })
                output.append({
                    "type": "custom", "emoji": "◉", "count": count,
                    "document_id": str(document_id), "custom_emoji": custom,
                })
            elif ((paid_type := getattr(tl_types, "ReactionPaid", None)) is not None
                  and isinstance(reaction, paid_type)):
                output.append({"type": "paid", "emoji": "⭐", "count": count})
            else:
                output.append({"type": "unknown", "emoji": "◉", "count": count})
        return output

    async def library_history(self, source_id, before=None):
        source = self.library_source(source_id)
        if before is not None and (not isinstance(before, int) or before <= 0):
            raise InboxError("Некорректный cursor.")
        key = (source_id, before or 0)
        snapshot = self.library_snapshots.get(key)
        if snapshot and self.clock() - snapshot["fetched"] < 2:
            return snapshot["payload"]
        options = {"limit": LIBRARY_PAGE_SIZE + 1}
        if before:
            options["offset_id"] = before
        fetched = list(await self._peer_call(
            source, lambda peer: self.client.get_messages(peer, **options)
        ))
        has_older = len(fetched) > LIBRARY_PAGE_SIZE
        fetched = fetched[:LIBRARY_PAGE_SIZE]
        fetched.sort(key=lambda message: message.id)
        by_id = {message.id: message for message in fetched}
        payload = {
            "source": self._source_json(source),
            "messages": await self.serialize_many(
                fetched, None, by_id, source_id=source_id
            ),
            "next_before": min(by_id) if has_older and by_id else None,
        }
        self.library_snapshots[key] = {"fetched": self.clock(), "messages": by_id,
                                       "payload": payload}
        self.library_snapshots.move_to_end(key)
        while len(self.library_snapshots) > 40:
            self.library_snapshots.popitem(last=False)
        return payload

    async def digest_rows(self, start, end):
        """Read only explicitly selected, digest-eligible Library sources."""
        rows = []
        sources = [source for source in self.store.library_sources()
                   if source.library_enabled and not source.digest_excluded
                   and source.peer_id not in self.store.ignored()]
        for source in sources:
            messages = list(await self._peer_call(source, lambda peer: self.client.get_messages(
                peer, limit=5000, offset_date=end,
            )))
            for message in messages:
                date = getattr(message, "date", None)
                if date is None or not (start <= date <= end):
                    continue
                action = getattr(message, "action", None)
                file = getattr(message, "file", None)
                raw_text = getattr(message, "raw_text", None) or ""
                sender = getattr(message, "sender", None)
                forwarded = getattr(message, "fwd_from", None)
                forward_identity = None
                if forwarded is not None:
                    origin = getattr(forwarded, "from_id", None)
                    original_id = getattr(forwarded, "channel_post", None)
                    if origin is not None and original_id is not None:
                        forward_identity = f"{origin!s}:{original_id}"
                rows.append({
                    "source_id": source.id, "source_title": source.display_title,
                    "message_id": int(message.id), "timestamp": date.isoformat(),
                    "sender": display_name(sender) if sender else "", "text": raw_text,
                    "caption": raw_text if file else "", "service": action is not None,
                    "sticker_only": bool(getattr(message, "sticker", None) and not raw_text),
                    "forward_identity": forward_identity,
                })
        return rows

    async def library_dialogs(self, query=""):
        """Load Telegram dialogs only for an explicit management request.

        The browser receives short-lived opaque tokens, never peer IDs or hashes.
        """
        if not isinstance(query, str) or len(query) > 100:
            raise InboxError("Некорректный поиск чатов.")
        self._check_telegram_cooldown()
        iterator = getattr(self.client, "iter_dialogs", None)
        if iterator is None:
            raise InboxError("Список чатов сейчас недоступен.", 503)
        self.dialog_tokens.clear()
        existing = {
            row.peer_id: row for row in self.store.library_sources(enabled_only=False)
        }
        needle = query.strip().casefold()
        dialogs = []
        try:
            async for dialog in iterator(limit=200):
                entity = getattr(dialog, "entity", None)
                if entity is None or getattr(entity, "id", None) == self.self_id:
                    continue
                title = (getattr(dialog, "name", None) or display_name(entity)).strip()
                if needle and needle not in title.casefold():
                    continue
                input_peer = getattr(dialog, "input_entity", None)
                peer_type, marked, access_hash = self._input_identity(
                    input_peer, dialog.id, entity
                )
                if peer_type not in {"user", "chat", "channel"}:
                    continue
                token = secrets.token_urlsafe(24)
                selected = existing.get(marked)
                candidate = {
                    "peer_type": peer_type,
                    "peer_id": marked,
                    "access_hash": access_hash,
                    "display_title": title[:512],
                    "is_bot": bool(getattr(entity, "bot", False)),
                    "source_id": selected.id if selected else None,
                    "expires": self.clock() + 300,
                }
                self.dialog_tokens[token] = candidate
                dialogs.append({
                    "token": token,
                    "source_id": selected.id if selected else None,
                    "title": candidate["display_title"],
                    "is_bot": candidate["is_bot"],
                    "selected": bool(selected and selected.library_enabled),
                    "notifications_muted": bool(
                        selected and selected.notifications_muted
                    ),
                    "allow_bot_write": bool(selected and selected.allow_bot_write),
                    "digest_excluded": bool(selected and selected.digest_excluded),
                    "sort_order": int(selected.sort_order) if selected else None,
                })
        except FloodWaitError as exc:
            raise self._flood_wait_error(exc) from None
        except RPCError:
            raise InboxError("Не удалось загрузить список Telegram-чатов.", 503) from None
        return {"dialogs": dialogs}

    async def _verify_source_bot(self, source):
        if isinstance(source, LibraryChat):
            return False
        getter = getattr(self.client, "get_entity", None)
        if getter is None:
            return bool(source.is_bot)
        try:
            entity = await self._peer_call(source, lambda peer: getter(peer))
        except InboxError:
            raise
        is_bot = isinstance(entity, User) and bool(getattr(entity, "bot", False))
        self.store.remember_peer(
            source.peer_type,
            source.peer_id,
            getattr(entity, "access_hash", source.access_hash),
            title=display_name(entity),
            is_bot=is_bot,
        )
        return is_bot

    async def update_library(self, identifier, preferences=None, **changes):
        async with self.action_lock:
            return await self._update_library(identifier, preferences, **changes)

    async def _update_library(self, identifier, preferences=None, **changes):
        if preferences is not None:
            if not isinstance(preferences, dict):
                raise InboxError("Некорректные настройки библиотеки.")
            changes = {**preferences, **changes}
        allowed = {
            "library_enabled", "sort_order", "notifications_muted",
            "allow_bot_write", "digest_excluded",
        }
        if set(changes) - allowed:
            raise InboxError("Некорректные настройки библиотеки.")
        for key in allowed - {"sort_order"}:
            if key in changes and not isinstance(changes[key], bool):
                raise InboxError("Некорректные настройки библиотеки.")
        if "sort_order" in changes and (
            not isinstance(changes["sort_order"], int)
            or not 0 <= changes["sort_order"] <= 10_000
        ):
            raise InboxError("Некорректный порядок библиотеки.")

        source = self.store.library_source(identifier, enabled_only=False)
        candidate = self.dialog_tokens.get(identifier)
        if source is None and candidate and candidate.get("source_id"):
            source = self.store.library_source(
                candidate["source_id"], enabled_only=False
            )
        if source is None and (
            candidate is None or candidate["expires"] < self.clock()
        ):
            raise InboxError("Обновите список Telegram-чатов и повторите.", 404)
        if source is None:
            requested_write = bool(changes.get("allow_bot_write", False))
            if requested_write and not candidate["is_bot"]:
                raise InboxError("Писать можно только явно выбранному Telegram-боту.", 422)
            source = self.store.upsert_library_source(
                peer_type=candidate["peer_type"],
                peer_id=candidate["peer_id"],
                access_hash=candidate["access_hash"],
                display_title=candidate["display_title"],
                is_bot=candidate["is_bot"],
                library_enabled=changes.get("library_enabled", True),
                notifications_muted=changes.get("notifications_muted", False),
                allow_bot_write=requested_write,
                digest_excluded=changes.get("digest_excluded", False),
                sort_order=changes.get("sort_order"),
            )
        else:
            if changes.get("allow_bot_write") and not await self._verify_source_bot(source):
                raise InboxError("Писать можно только Telegram-боту.", 422)
            source = self.store.update_library_source(source.id, **changes)
        self.library_snapshots.clear()
        return self._source_json(source)

    def reorder_library(self, source_ids):
        if not isinstance(source_ids, list) or not all(
            isinstance(item, str) for item in source_ids
        ):
            raise InboxError("Некорректный порядок библиотеки.")
        if not self.store.reorder_library(source_ids):
            raise InboxError("Список источников изменился; обновите страницу.", 409)
        return {"sources": self.library_json()}

    def open_library(self, source_id):
        source = self.library_source(source_id)
        latest = int(getattr(source, "last_seen_message_id", 0) or 0)
        opened_conversation_ids = []
        for row in self.store.active():
            if row.library_source_id == source_id:
                latest = max(latest, int(row.latest_relevant_message_id or 0))
                opened = self.store.open(row.id)
                if opened is not None:
                    opened_conversation_ids.append(opened.id)
        if not isinstance(source, LibraryChat):
            source = self.store.mark_library_seen(source_id, latest)
        return {
            "source": self._source_json(source),
            "opened_conversation_ids": opened_conversation_ids,
        }

    async def library_pins(self, source_id):
        source = self.library_source(source_id)
        key = ("library", source_id)
        snapshot = self.pin_snapshots.get(key)
        if snapshot and self.clock() - snapshot["fetched"] < PIN_CACHE_SECONDS:
            return snapshot["payload"]
        fetched = list(await self._peer_call(source, lambda peer: self.client.get_messages(
            peer, limit=20, filter=InputMessagesFilterPinned()
        )))
        fetched.sort(key=lambda message: message.id)
        by_id = {message.id: message for message in fetched}
        payload = {"pins": await self.serialize_many(
            fetched, None, by_id, source_id=source_id
        )}
        self.pin_snapshots[key] = {"fetched": self.clock(), "payload": payload}
        return payload

    async def library_message(self, source_id, message_id):
        source = self.library_source(source_id)
        if not isinstance(message_id, int) or message_id <= 0:
            raise InboxError("Некорректное сообщение.")
        message = await self._peer_call(
            source, lambda peer: self.client.get_messages(peer, ids=message_id)
        )
        if not message:
            raise InboxError("Сообщение не найдено в этом источнике.", 404)
        serialized = await self.serialize_library(message, source_id, {message.id: message})
        if serialized is None:
            raise InboxError("Сообщение не содержит отображаемых данных.", 404)
        return {"source": self._source_json(source), "message": serialized}

    async def library_search(self, source_id, query, before=None):
        source = self.library_source(source_id)
        if not isinstance(query, str) or not 2 <= len(query.strip()) <= 100:
            raise InboxError("Введите не меньше двух символов.")
        if before is not None and (not isinstance(before, int) or before <= 0):
            raise InboxError("Некорректный cursor.")
        options = {"limit": SEARCH_PAGE_SIZE + 1, "search": query.strip()}
        if before:
            options["offset_id"] = before
        fetched = list(await self._peer_call(
            source, lambda peer: self.client.get_messages(peer, **options)
        ))
        has_more = len(fetched) > SEARCH_PAGE_SIZE
        fetched = fetched[:SEARCH_PAGE_SIZE]
        fetched.sort(key=lambda message: message.id, reverse=True)
        by_id = {message.id: message for message in fetched}
        return {
            "results": await self.serialize_many(
                fetched, None, by_id, source_id=source_id
            ),
            "next_before": min(by_id) if has_more and by_id else None,
        }

    async def conversation_pins(self, key):
        row = self.require(key)
        cache_key = ("inbox", key)
        snapshot = self.pin_snapshots.get(cache_key)
        if snapshot and self.clock() - snapshot["fetched"] < PIN_CACHE_SECONDS:
            return snapshot["payload"]
        fetched = list(await self._peer_call(
            row,
            lambda peer: self.client.get_messages(
                peer, limit=20, filter=InputMessagesFilterPinned()
            ),
            quarantine_key=key,
        ))
        fetched = [message for message in fetched if await self.belongs(message, row)]
        self._allow_conversation_media(key, fetched)
        fetched.sort(key=lambda message: message.id)
        by_id = {message.id: message for message in fetched}
        payload = {"pins": await self.serialize_many(fetched, row, by_id)}
        self.pin_snapshots[cache_key] = {"fetched": self.clock(), "payload": payload}
        return payload

    async def conversation_message(self, key, message_id):
        row = self.require(key)
        if not isinstance(message_id, int) or message_id <= 0:
            raise InboxError("Некорректное сообщение.")
        message = await self._peer_call(
            row,
            lambda peer: self.client.get_messages(peer, ids=message_id),
            quarantine_key=key,
        )
        if not message or not await self.belongs(message, row):
            raise InboxError("Сообщение не принадлежит этому разговору.", 404)
        self._allow_conversation_media(key, [message])
        serialized = await self.serialize(message, row, {message.id: message})
        if serialized is None:
            raise InboxError("Сообщение не содержит отображаемых данных.", 404)
        return {"conversation": conversation_json(row), "message": serialized}

    async def conversation_search(self, key, query, before=None):
        row = self.require(key)
        if not isinstance(query, str) or not 2 <= len(query.strip()) <= 100:
            raise InboxError("Введите не меньше двух символов.")
        if before is not None and (not isinstance(before, int) or before <= 0):
            raise InboxError("Некорректный cursor.")
        options = {"limit": SEARCH_PAGE_SIZE + 1, "search": query.strip()}
        if before:
            options["offset_id"] = before
        fetched = list(await self._peer_call(
            row,
            lambda peer: self.client.get_messages(peer, **options),
            quarantine_key=key,
        ))
        fetched = [message for message in fetched if await self.belongs(message, row)]
        self._allow_conversation_media(key, fetched)
        has_more = len(fetched) > SEARCH_PAGE_SIZE
        fetched = fetched[:SEARCH_PAGE_SIZE]
        fetched.sort(key=lambda message: message.id, reverse=True)
        by_id = {message.id: message for message in fetched}
        return {
            "results": await self.serialize_many(fetched, row, by_id),
            "next_before": min(by_id) if has_more and by_id else None,
        }

    async def belongs(self, message, row):
        if row.is_forum:
            return message.id == row.thread_id or forum_thread(message) == row.thread_id
        if not row.thread_id:
            return True
        if message.id == row.thread_id:
            return True
        thread, _ = await thread_context(message, None)
        return thread == row.thread_id

    async def serialize(self, message, row, by_id):
        sender = getattr(message, "sender", None)
        raw_text = getattr(message, "raw_text", None) or ""
        system = service_message_text(message)
        if system is not None:
            return {
                "id": message.id, "text": "", "own": False, "segments": [],
                "sender": "", "timestamp": message.date.isoformat(), "mention": False,
                "reply": None, "media": None, "system": system,
            }
        file = getattr(message, "file", None)
        if not raw_text and not file:
            return None
        result = {
            "id": message.id, "text": raw_text, "own": bool(message.out),
            "segments": rich_text_segments(raw_text, getattr(message, "entities", None),
                                            self._custom_emoji_payloads(
                                                getattr(message, "entities", None))),
            "sender": display_name(sender) if sender else "Неизвестный отправитель",
            "timestamp": message.date.isoformat(),
            "mention": has_exact_fedocc_mention(raw_text) and not message.out,
            "reply": None, "media": None, "system": None,
            "reactions": self._reaction_payloads(message),
        }
        reply_id = getattr(message, "reply_to_msg_id", None)
        if reply_id:
            parent = by_id.get(reply_id)
            if parent is None:
                parent = await message.get_reply_message()
            if parent and await self.belongs(parent, row):
                reply_text = (parent.raw_text or "[Вложение]")[:500]
                result["reply"] = {"sender": display_name(parent.sender) if parent.sender else "",
                                   "text": reply_text,
                                   "segments": safe_link_segments(
                                       reply_text, getattr(parent, "entities", None))}
        if file:
            kind = "file"
            sticker_format = None
            if getattr(message, "sticker", None):
                mime_type = (file.mime_type or "").lower()
                kind = "sticker"
                if mime_type.startswith("image/"):
                    sticker_format = "static"
                elif mime_type == "video/webm":
                    sticker_format = "video"
                elif mime_type == "application/x-tgsticker":
                    sticker_format = "animated"
                else:
                    sticker_format = "unsupported"
            elif message.photo:
                kind = "photo"
            elif message.voice:
                kind = "voice"
            elif message.audio:
                kind = "audio"
            elif message.video_note:
                # video also matches round videos; test the narrower property first.
                kind = "video_note"
            elif message.video:
                kind = "video"
            result["media"] = {
                "kind": kind, "name": file.name or {"photo": "Фото", "voice": "Голосовое",
                    "audio": "Аудиофайл", "video_note": "Видеосообщение",
                    "video": "Видео", "sticker": "Стикер"}.get(kind, "Файл"),
                "size": file.size or 0, "duration": file.duration or 0,
                "url": f"/api/conversations/{row.id}/media/{message.id}",
                "available": bool(file.size and file.size <= MAX_MEDIA),
            }
            if sticker_format is not None:
                result["media"]["sticker_format"] = sticker_format
        return result

    async def history(self, key):
        async with self.history_lock:
            row = self.require(key)
            snapshot = self.snapshots.get(key)
            if snapshot and self.clock() - snapshot["fetched"] < 2:
                return snapshot["payload"]
            return await self._peer_call(
                row,
                lambda peer: self._history_for_peer(key, row, snapshot, peer),
                quarantine_key=key,
            )

    async def _history_for_peer(self, key, row, snapshot, peer):
        if row.thread_id and not row.topic_title:
            root = await self.client.get_messages(peer, ids=row.thread_id)
            action = getattr(root, "action", None)
            title = getattr(action, "title", None)
            if row.is_forum and row.thread_id == 1:
                title = title or "Общая тема"
            if title:
                with self.store.factory() as session:
                    from app.db.tables import InboxConversation

                    record = session.get(InboxConversation, key)
                    record.topic_title = title[:512]
                    session.commit()
                row.topic_title = title[:512]
        options = {"reply_to": row.thread_id} if row.thread_id and not (
            row.is_forum and row.thread_id == 1) else {}
        if snapshot and snapshot["trigger"] == row.trigger_id:
            by_id = dict(snapshot["messages"])
            last_id = max(snapshot["cursor"], max(by_id, default=row.trigger_id))
            fetched = await self.client.get_messages(
                peer, limit=100, min_id=last_id, reverse=True, **options,
            )
        else:
            by_id = {}
            before = []
            offset = row.trigger_id + 1
            for _ in range(20 if row.is_forum and row.thread_id == 1 else 1):
                page = await self.client.get_messages(
                    peer, limit=100, offset_id=offset, **options,
                )
                before.extend([m for m in page if await self.belongs(m, row)])
                if len(before) >= 100 or len(page) < 100:
                    break
                offset = min(m.id for m in page)
            before = before[:100]
            after = await self.client.get_messages(
                peer, limit=100, min_id=row.trigger_id, reverse=True, **options,
            )
            trigger = await self.client.get_messages(peer, ids=row.trigger_id)
            fetched = [*before, *after, *([trigger] if trigger else [])]
        for message in fetched:
            if message and await self.belongs(message, row):
                by_id[message.id] = message
        # In General, pagination must advance past other topics too.
        cursor = max([m.id for m in fetched if m] + [snapshot.get("cursor", 0)
                     if snapshot else row.trigger_id])
        by_id = dict(sorted(by_id.items())[-500:])
        payload = {"conversation": conversation_json(row), "messages": (
            await self.serialize_many(by_id.values(), row, by_id)
        )}
        self.require(key)  # Never return data after a concurrent close/expiry.
        self.snapshots[key] = {"fetched": self.clock(), "messages": by_id,
            "payload": payload, "trigger": row.trigger_id, "cursor": cursor}
        self.snapshots.move_to_end(key)
        while len(self.snapshots) > 20:
            self.snapshots.popitem(last=False)
        return payload

    async def close(self, key):
        async with self.action_lock:
            self.require(key)
            self.store.close(key)
            async with self.media_lock:
                await self.cleanup()

    async def validate_reply(self, peer, reply_to, row=None):
        if reply_to is None:
            return None
        if not isinstance(reply_to, int) or reply_to <= 0:
            raise InboxError("Некорректное сообщение для ответа.")
        message = await self.client.get_messages(peer, ids=reply_to)
        if not message or (row is not None and not await self.belongs(message, row)):
            raise InboxError("Сообщение для ответа не принадлежит этому разговору.", 422)
        return reply_to

    async def _claim_send(self, request_id, scope, *, insert=True):
        with self.store.factory() as session:
            previous = session.get(InboxSend, request_id)
            if previous:
                if previous.conversation_id != scope:
                    raise InboxError("Идентификатор отправки уже использован.", 409)
                if previous.status == "sent":
                    return {"message_id": previous.message_id}
                raise InboxError(
                    "Статус отправки неизвестен. Проверьте сообщения перед повтором.", 409,
                )
            if insert:
                session.add(InboxSend(request_id=request_id, conversation_id=scope,
                                      created_at=self.clock(), status="pending"))
                session.commit()
        return None

    def _finish_send(self, request_id, sent):
        with self.store.factory() as session:
            record = session.get(InboxSend, request_id)
            record.status = "sent"
            record.message_id = sent.id
            session.commit()

    async def send(self, key, request_id, text, image=None, *, file_path=None,
                   filename=None, mime_type=None, reply_to=None):
        async with self.action_lock:
            previous = await self._claim_send(request_id, key, insert=False)
            if previous:
                return previous
            row = self.require(key)
            if row.library_source_id or self.store.library_source_for_peer(row.peer_id):
                raise InboxError(
                    "Отправляйте сообщения выбранному источнику через Библиотеку.", 403
                )
            if not self.client.is_connected():
                raise InboxError("Telegram не подключён. Черновик сохранён.", 503)
            self._check_telegram_cooldown()
            if not isinstance(text, str) or len(text.encode("utf-16-le")) // 2 > (
                1024 if image is not None or file_path is not None else 4096
            ):
                raise InboxError("Слишком длинное сообщение (4096 символов; с фото — 1024).")
            if image is None and file_path is None and not text.strip():
                raise InboxError("Введите сообщение или прикрепите файл.")
            photo = await asyncio.to_thread(normalize_image, image) if image is not None else None
            attachment, force_document = await self._attachment(file_path, mime_type)
            self.require(key)
            await self._claim_send(request_id, key)

            async def send_to_conversation(peer):
                target_reply = await self.validate_reply(peer, reply_to, row)
                default_reply = row.thread_id or None
                async with asyncio.timeout(180):
                    if photo is None and file_path is None:
                        return await self.client.send_message(
                            peer, text, reply_to=target_reply or default_reply,
                            parse_mode=None, link_preview=False,
                        )
                    outbound = photo or attachment
                    return await self.client.send_file(
                        peer, outbound, caption=text,
                        reply_to=target_reply or default_reply, parse_mode=None,
                        force_document=force_document if photo is None else False,
                        mime_type=mime_type,
                        attributes=(self._file_attributes(filename)
                                    if photo is None and force_document else None),
                    )

            try:
                sent = await self._peer_call(
                    row,
                    send_to_conversation,
                    quarantine_key=key,
                )
            except InboxError:
                with self.store.factory() as session:
                    session.execute(delete(InboxSend).where(
                        InboxSend.request_id == request_id
                    ))
                    session.commit()
                raise
            except FloodWaitError as exc:
                with self.store.factory() as session:
                    session.execute(delete(InboxSend).where(
                        InboxSend.request_id == request_id
                    ))
                    session.commit()
                raise self._flood_wait_error(exc) from None
            except RPCError:
                with self.store.factory() as session:
                    session.execute(delete(InboxSend).where(InboxSend.request_id == request_id))
                    session.commit()
                raise InboxError("Telegram отклонил отправку: проверьте права, лимиты или тему. "
                                 "Черновик сохранён.", 422) from None
            except (OSError, TimeoutError):
                raise InboxError("Связь прервалась. Статус отправки неизвестен; "
                                 "проверьте сообщения. Черновик сохранён.", 409) from None
            self._finish_send(request_id, sent)
            self.store.extend(key)
            if key in self.snapshots:
                self.snapshots[key]["fetched"] = 0
            return {"message_id": sent.id}

    @staticmethod
    def _file_attributes(filename):
        if not filename:
            return None
        from telethon.tl.types import DocumentAttributeFilename

        return [DocumentAttributeFilename(Path(filename).name[:180])]

    async def _attachment(self, file_path, mime_type):
        if not file_path or not (mime_type or "").startswith("image/"):
            return file_path, True
        path = Path(file_path)
        if path.stat().st_size > MAX_IMAGE:
            raise InboxError("Изображение должно быть не больше 10 МБ.")
        raw = await asyncio.to_thread(path.read_bytes)
        return await asyncio.to_thread(normalize_image, raw), False

    async def send_saved(self, request_id, text, *, file_path=None, filename=None,
                         mime_type=None):
        scope = "library:saved"
        async with self.action_lock:
            previous = await self._claim_send(request_id, scope, insert=False)
            if previous:
                return previous
            if not self.client.is_connected():
                raise InboxError("Telegram не подключён. Черновик сохранён.", 503)
            self._check_telegram_cooldown()
            if not isinstance(text, str) or len(text.encode("utf-16-le")) // 2 > (
                    1024 if file_path else 4096):
                raise InboxError("Слишком длинное сообщение.")
            if not text.strip() and file_path is None:
                raise InboxError("Введите сообщение или прикрепите файл.")
            # Current AyuGram dev uses 12s for text and 12 + upload estimate + 1
            # for files. Scheduling is server-side and survives this request.
            size = Path(file_path).stat().st_size if file_path else 0
            delay = (12 if not file_path else 13 + math.ceil(max(
                6, math.ceil(size / (1024 * 1024) * 0.7),
            )))
            schedule = timedelta(seconds=delay)
            attachment, force_document = await self._attachment(file_path, mime_type)
            await self._claim_send(request_id, scope)
            try:
                async with asyncio.timeout(180):
                    if file_path is None:
                        sent = await self.client.send_message(
                            "me", text, parse_mode=None, link_preview=False, schedule=schedule,
                        )
                    else:
                        sent = await self.client.send_file(
                            "me", attachment, caption=text, parse_mode=None,
                            force_document=force_document, mime_type=mime_type,
                            attributes=(self._file_attributes(filename)
                                        if force_document else None),
                            schedule=schedule,
                        )
            except FloodWaitError as exc:
                with self.store.factory() as session:
                    session.execute(delete(InboxSend).where(
                        InboxSend.request_id == request_id
                    ))
                    session.commit()
                raise self._flood_wait_error(exc) from None
            except RPCError:
                with self.store.factory() as session:
                    session.execute(delete(InboxSend).where(InboxSend.request_id == request_id))
                    session.commit()
                raise InboxError("Telegram отклонил запланированную отправку.", 422) from None
            except (OSError, TimeoutError):
                raise InboxError(
                    "Статус отправки неизвестен; проверьте Saved Messages.", 409,
                ) from None
            self._finish_send(request_id, sent)
            self.library_snapshots.clear()
            return {"message_id": sent.id, "scheduled_in": delay}

    async def send_library(
        self, source_id, request_id, text, image=None, *, file_path=None,
        filename=None, mime_type=None, reply_to=None,
    ):
        if source_id == SAVED_MESSAGES.id:
            if image is not None:
                raise InboxError("Для Избранного используйте загрузку файла.")
            return await self.send_saved(
                request_id, text, file_path=file_path, filename=filename,
                mime_type=mime_type,
            )
        async with self.action_lock:
            # Reload after acquiring the shared permission/send boundary. A
            # queued disable must take effect before any later queued send.
            source = self.library_source(source_id)
            scope = f"library:{source.id}"
            previous = await self._claim_send(request_id, scope, insert=False)
            if previous:
                return previous
            if not source.library_enabled or not source.allow_bot_write:
                raise InboxError("Отправка этому источнику не разрешена.", 403)
            if not await self._verify_source_bot(source):
                self.store.update_library_source(source.id, allow_bot_write=False)
                raise InboxError("Источник больше не является Telegram-ботом.", 403)
            if not self.client.is_connected():
                raise InboxError("Telegram не подключён. Черновик сохранён.", 503)
            if not isinstance(text, str) or len(text.encode("utf-16-le")) // 2 > (
                1024 if image is not None or file_path is not None else 4096
            ):
                raise InboxError("Слишком длинное сообщение.")
            if image is None and file_path is None and not text.strip():
                raise InboxError("Введите сообщение или прикрепите файл.")
            photo = await asyncio.to_thread(normalize_image, image) if image is not None else None
            attachment, force_document = await self._attachment(file_path, mime_type)
            await self._claim_send(request_id, scope)

            async def send_to_bot(peer):
                target_reply = await self.validate_reply(peer, reply_to)
                async with asyncio.timeout(180):
                    if photo is None and file_path is None:
                        return await self.client.send_message(
                            peer, text, reply_to=target_reply, parse_mode=None,
                            link_preview=False,
                        )
                    return await self.client.send_file(
                        peer, photo or attachment, caption=text, reply_to=target_reply,
                        parse_mode=None,
                        force_document=force_document if photo is None else False,
                        mime_type=mime_type,
                        attributes=(self._file_attributes(filename)
                                    if photo is None and force_document else None),
                    )

            try:
                sent = await self._peer_call(source, send_to_bot)
            except InboxError:
                with self.store.factory() as session:
                    session.execute(delete(InboxSend).where(
                        InboxSend.request_id == request_id
                    ))
                    session.commit()
                raise
            except RPCError:
                with self.store.factory() as session:
                    session.execute(delete(InboxSend).where(
                        InboxSend.request_id == request_id
                    ))
                    session.commit()
                raise InboxError(
                    "Telegram отклонил отправку боту. Черновик сохранён.", 422
                ) from None
            except (OSError, TimeoutError):
                raise InboxError(
                    "Статус отправки неизвестен; проверьте чат бота.", 409
                ) from None
            self._finish_send(request_id, sent)
            self.library_snapshots.clear()
            return {"message_id": sent.id}

    async def media(self, key, message_id):
        async with self.media_lock:
            row = self.require(key)
            snapshot = self.snapshots.get(key)
            allowance = self.media_allowances.get((key, message_id), 0)
            if allowance <= self.clock():
                self.media_allowances.pop((key, message_id), None)
                allowance = 0
            if (
                (not snapshot or message_id not in snapshot["messages"])
                and not allowance
            ):
                raise InboxError("Откройте разговор, чтобы загрузить вложение.", 404)
            message = await self._peer_call(
                row,
                lambda peer: self.client.get_messages(peer, ids=message_id),
                quarantine_key=key,
            )
            if not message or not await self.belongs(message, row) or not message.file:
                raise InboxError("Вложение недоступно.", 404)
            size = message.file.size
            if not size or size > MAX_MEDIA:
                raise InboxError("Вложение превышает лимит 64 МБ.", 413)
            path = self.cache_dir / f"{row.id}_{message_id}.bin"
            if path.is_symlink():
                raise InboxError("Вложение недоступно.", 404)
            if not path.exists():
                await self.cleanup(reserve=size)
                temporary = path.with_suffix(".part")
                def progress(current, total):
                    if current > min(MAX_MEDIA, size) or total > min(MAX_MEDIA, size):
                        raise InboxError("Вложение превышает лимит 64 МБ.", 413)
                try:
                    with temporary.open("xb") as output:
                        temporary.chmod(0o600)
                        async with asyncio.timeout(90):
                            await self.client.download_media(message, file=output,
                                                             progress_callback=progress)
                    temporary.replace(path)
                except FloodWaitError as exc:
                    temporary.unlink(missing_ok=True)
                    raise self._flood_wait_error(exc) from None
                except BaseException:
                    temporary.unlink(missing_ok=True)
                    raise
            self.require(key)
            mime = message.file.mime_type or "application/octet-stream"
            inline = mime in INLINE_MEDIA_TYPES
            return path, mime if inline else "application/octet-stream", message.file.name, inline

    def _allow_conversation_media(self, key, messages):
        expires = self.clock() + MEDIA_ALLOW_SECONDS
        for message in messages:
            if not getattr(message, "file", None):
                continue
            token = (key, int(message.id))
            self.media_allowances[token] = expires
            self.media_allowances.move_to_end(token)
        while len(self.media_allowances) > 500:
            self.media_allowances.popitem(last=False)

    async def library_media(self, source_id, message_id):
        async with self.media_lock:
            source = self.library_source(source_id)
            message = await self._peer_call(
                source, lambda peer: self.client.get_messages(peer, ids=message_id)
            )
            if not message or not message.file:
                raise InboxError("Вложение недоступно.", 404)
            size = message.file.size
            if not size or size > MAX_MEDIA:
                raise InboxError("Вложение превышает лимит 64 МБ.", 413)
            path = self.cache_dir / f"library_{source_id}_{message_id}.bin"
            if path.is_symlink():
                raise InboxError("Вложение недоступно.", 404)
            if not path.exists():
                await self.cleanup(reserve=size)
                temporary = path.with_suffix(".part")

                def progress(current, total):
                    if current > min(MAX_MEDIA, size) or total > min(MAX_MEDIA, size):
                        raise InboxError("Вложение превышает лимит 64 МБ.", 413)

                try:
                    with temporary.open("xb") as output:
                        temporary.chmod(0o600)
                        async with asyncio.timeout(90):
                            await self.client.download_media(
                                message, file=output, progress_callback=progress,
                            )
                    temporary.replace(path)
                except FloodWaitError as exc:
                    temporary.unlink(missing_ok=True)
                    raise self._flood_wait_error(exc) from None
                except BaseException:
                    temporary.unlink(missing_ok=True)
                    raise
            self.library_source(source_id)  # Revalidate after a concurrent preference change.
            mime = message.file.mime_type or "application/octet-stream"
            inline = mime in INLINE_MEDIA_TYPES
            return (
                path,
                mime if inline else "application/octet-stream",
                message.file.name,
                inline,
            )

    async def custom_emoji_media(self, document_id):
        document = self.custom_emoji_documents.get(int(document_id))
        if document is None:
            raise InboxError("Emoji недоступен.", 404)
        mime = (getattr(document, "mime_type", "") or "").lower()
        suffix = {"image/png": ".png", "image/webp": ".webp", "image/jpeg": ".jpg",
                  "video/webm": ".webm", "application/x-tgsticker": ".tgs"}.get(mime)
        size = int(getattr(document, "size", 0) or 0)
        if suffix is None or not size or size > MAX_MEDIA:
            raise InboxError("Emoji недоступен.", 404)
        path = self.cache_dir / f"custom-emoji-{int(document_id)}{suffix}"
        async with self.media_lock:
            if not path.exists():
                await self.cleanup(reserve=size)
                temporary = path.with_suffix(path.suffix + ".part")
                try:
                    with temporary.open("xb") as output:
                        temporary.chmod(0o600)
                        async with asyncio.timeout(30):
                            await self.client.download_media(document, file=output)
                    temporary.replace(path)
                except BaseException:
                    temporary.unlink(missing_ok=True)
                    raise
        return path, mime, path.name, mime != "application/x-tgsticker"

    async def cleanup(self, reserve=0):
        self.store.cleanup()
        for partial in self.cache_dir.glob("*.part"):
            partial.unlink(missing_ok=True)
        for partial in self.upload_dir.glob("*.upload"):
            if partial.stat().st_mtime < self.clock() - self.upload_stale:
                partial.unlink(missing_ok=True)
        active = {row.id for row in self.store.active()}
        for key in list(self.snapshots):
            if key not in active:
                del self.snapshots[key]
        for token, expires in list(self.media_allowances.items()):
            if token[0] not in active or expires <= self.clock():
                del self.media_allowances[token]
        files = sorted(
            (path for path in self.cache_dir.iterdir()
             if path.is_file() and not path.name.endswith(".part")),
            key=lambda path: path.stat().st_mtime,
        )
        total = sum(p.stat().st_size for p in files)
        for path in files:
            scoped_inactive = (
                not path.stem.startswith(("library_", "custom-emoji-"))
                and path.stem.split("_")[0] not in active
            )
            if scoped_inactive or total + reserve > MAX_CACHE:
                total -= path.stat().st_size
                path.unlink(missing_ok=True)
        with self.store.factory() as session:
            session.execute(delete(InboxSend).where(InboxSend.created_at < self.clock() - 86400))
            session.commit()
