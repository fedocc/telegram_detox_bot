from __future__ import annotations

import asyncio
import io
import math
import time
import warnings
from collections import OrderedDict
from datetime import timedelta
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError
from sqlalchemy import delete
from telethon.errors import RPCError
from telethon.tl.types import PeerChannel

from app.db.tables import InboxSend
from app.inbox.library import SAVED_MESSAGES
from app.inbox.store import InboxStore
from app.services.attention import INBOX_TRIGGER_TYPES, classify_incoming
from app.services.mentions import has_exact_fedocc_mention
from app.telegram.mapper import display_name

MAX_IMAGE = 10 * 1024 * 1024
MAX_MEDIA = 64 * 1024 * 1024
MAX_CACHE = 256 * 1024 * 1024
LIBRARY_PAGE_SIZE = 50


class InboxError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def forum_thread(message):
    header = getattr(message, "reply_to", None)
    if header and getattr(header, "forum_topic", False):
        return header.reply_to_top_id or header.reply_to_msg_id or 1
    return 1


async def thread_context(message, chat):
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
    )} | {"thread_id": row.thread_id,
          "expires_at": row.expires_at if row.opened_at is not None else None}


class InboxService:
    def __init__(self, client, factory, ignored, cache_dir, self_id, clock=time.time,
                 library=None, upload_max_mb=100, upload_concurrency=2,
                 upload_stale_hours=24):
        self.client = client
        self.store = InboxStore(factory, ignored, clock)
        self.store.clamp_existing_lifetimes()
        self.self_id = self_id
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.snapshots = OrderedDict()
        self.history_lock = asyncio.Lock()
        self.action_lock = asyncio.Lock()
        self.media_lock = asyncio.Lock()
        self.library = tuple(library or (SAVED_MESSAGES,))
        self.library_by_id = {row.id: row for row in self.library}
        self.library_snapshots = OrderedDict()
        self.upload_max = upload_max_mb * 1024 * 1024
        self.upload_stale = upload_stale_hours * 3600
        self.upload_dir = self.cache_dir.parent / "outbox_tmp"
        self.upload_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.upload_slots = asyncio.Semaphore(upload_concurrency)
        self.clock = clock

    def require(self, key):
        row = self.store.get(key)
        if row is None:
            raise InboxError("Разговор закрыт или время активности истекло.", 404)
        return row

    async def observe(self, event, *, trigger=None):
        if (str(event.chat_id) in self.store.ignored() or event.out
                or event.sender_id == self.self_id):
            return
        if trigger is None:
            trigger = await classify_incoming(event.message, self_id=self.self_id,
                                               text=event.raw_text, sender_id=event.sender_id)
        if trigger not in INBOX_TRIGGER_TYPES:
            return
        chat = await event.get_chat()
        thread, forum = await thread_context(event.message, chat)
        self.store.activate(
            peer_id=event.chat_id, thread_id=thread, is_forum=forum,
            title=display_name(chat), trigger_id=event.id, preview=event.raw_text or "",
            reason=trigger,
        )

    def library_json(self):
        return [{"id": row.id, "title": row.title, "writable": row.writable}
                for row in self.library]

    def library_source(self, source_id):
        source = self.library_by_id.get(source_id)
        if source is None:
            raise InboxError("Раздел библиотеки не найден.", 404)
        return source

    async def serialize_library(self, message, source_id, by_id):
        row = type("LibraryRow", (), {"id": source_id, "is_forum": False,
                                      "thread_id": 0})()
        result = await self.serialize(message, row, by_id)
        if result["media"]:
            result["media"]["url"] = f"/api/library/{source_id}/media/{message.id}"
        return result

    async def library_history(self, source_id, before=None):
        source = self.library_source(source_id)
        if before is not None and (not isinstance(before, int) or before <= 0):
            raise InboxError("Некорректный cursor.")
        key = (source_id, before or 0)
        snapshot = self.library_snapshots.get(key)
        if snapshot and self.clock() - snapshot["fetched"] < 2:
            return snapshot["payload"]
        options = {"limit": LIBRARY_PAGE_SIZE}
        if before:
            options["offset_id"] = before
        fetched = list(await self.client.get_messages(source.peer_id, **options))
        fetched.sort(key=lambda message: message.id)
        by_id = {message.id: message for message in fetched}
        payload = {
            "source": {"id": source.id, "title": source.title, "writable": source.writable},
            "messages": [await self.serialize_library(message, source_id, by_id)
                         for message in fetched],
            "next_before": min(by_id) if len(fetched) == LIBRARY_PAGE_SIZE else None,
        }
        self.library_snapshots[key] = {"fetched": self.clock(), "messages": by_id,
                                       "payload": payload}
        self.library_snapshots.move_to_end(key)
        while len(self.library_snapshots) > 40:
            self.library_snapshots.popitem(last=False)
        return payload

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
        sender = message.sender
        result = {
            "id": message.id, "text": message.raw_text or "", "own": bool(message.out),
            "sender": display_name(sender) if sender else "Неизвестный отправитель",
            "timestamp": message.date.isoformat(),
            "mention": has_exact_fedocc_mention(message.raw_text) and not message.out,
            "reply": None, "media": None,
        }
        reply_id = getattr(message, "reply_to_msg_id", None)
        if reply_id:
            parent = by_id.get(reply_id)
            if parent is None:
                parent = await message.get_reply_message()
            if parent and await self.belongs(parent, row):
                result["reply"] = {"sender": display_name(parent.sender) if parent.sender else "",
                                   "text": (parent.raw_text or "[Вложение]")[:500]}
        file = message.file
        if file:
            kind = "file"
            if message.photo:
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
                    "video": "Видео"}.get(kind, "Файл"),
                "size": file.size or 0, "duration": file.duration or 0,
                "url": f"/api/conversations/{row.id}/media/{message.id}",
                "available": bool(file.size and file.size <= MAX_MEDIA),
            }
        return result

    async def history(self, key):
        async with self.history_lock:
            row = self.require(key)
            snapshot = self.snapshots.get(key)
            if snapshot and self.clock() - snapshot["fetched"] < 2:
                return snapshot["payload"]
            peer = int(row.peer_id)
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
            payload = {"conversation": conversation_json(row), "messages": [
                await self.serialize(message, row, by_id) for message in by_id.values()
            ]}
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
            if not self.client.is_connected():
                raise InboxError("Telegram не подключён. Черновик сохранён.", 503)
            if not isinstance(text, str) or len(text.encode("utf-16-le")) // 2 > (
                1024 if image is not None or file_path is not None else 4096
            ):
                raise InboxError("Слишком длинное сообщение (4096 символов; с фото — 1024).")
            if image is None and file_path is None and not text.strip():
                raise InboxError("Введите сообщение или прикрепите файл.")
            photo = await asyncio.to_thread(normalize_image, image) if image is not None else None
            attachment, force_document = await self._attachment(file_path, mime_type)
            self.require(key)
            target_reply = await self.validate_reply(int(row.peer_id), reply_to, row)
            default_reply = row.thread_id or None
            await self._claim_send(request_id, key)
            try:
                async with asyncio.timeout(180):
                    if photo is None and file_path is None:
                        sent = await self.client.send_message(
                            int(row.peer_id), text, reply_to=target_reply or default_reply,
                            parse_mode=None, link_preview=False,
                        )
                    else:
                        attachment = photo or attachment
                        sent = await self.client.send_file(
                            int(row.peer_id), attachment, caption=text,
                            reply_to=target_reply or default_reply, parse_mode=None,
                            force_document=force_document if photo is None else False,
                            mime_type=mime_type,
                            attributes=(self._file_attributes(filename)
                                        if photo is None and force_document else None),
                        )
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

    async def media(self, key, message_id):
        async with self.media_lock:
            row = self.require(key)
            snapshot = self.snapshots.get(key)
            if not snapshot or message_id not in snapshot["messages"]:
                raise InboxError("Откройте разговор, чтобы загрузить вложение.", 404)
            message = await self.client.get_messages(int(row.peer_id), ids=message_id)
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
                except BaseException:
                    temporary.unlink(missing_ok=True)
                    raise
            self.require(key)
            mime = message.file.mime_type or "application/octet-stream"
            inline = mime in {"image/jpeg", "image/png", "image/webp", "video/mp4",
                              "video/webm", "audio/ogg", "audio/mpeg", "audio/mp4", "audio/wav"}
            return path, mime if inline else "application/octet-stream", message.file.name, inline

    async def library_media(self, source_id, message_id):
        source = self.library_source(source_id)
        message = await self.client.get_messages(source.peer_id, ids=message_id)
        if not message or not message.file:
            raise InboxError("Вложение недоступно.", 404)
        size = message.file.size
        if not size or size > MAX_MEDIA:
            raise InboxError("Вложение превышает лимит 64 МБ.", 413)
        path = self.cache_dir / f"library_{source_id}_{message_id}.bin"
        if path.is_symlink():
            raise InboxError("Вложение недоступно.", 404)
        if not path.exists():
            temporary = path.with_suffix(".part")
            try:
                with temporary.open("xb") as output:
                    temporary.chmod(0o600)
                    await self.client.download_media(message, file=output,
                                                     progress_callback=lambda *_: None)
                temporary.replace(path)
            except BaseException:
                temporary.unlink(missing_ok=True)
                raise
        mime = message.file.mime_type or "application/octet-stream"
        inline = mime.startswith(("image/", "video/", "audio/"))
        return path, mime if inline else "application/octet-stream", message.file.name, inline

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
        files = sorted(self.cache_dir.glob("*.bin"), key=lambda p: p.stat().st_mtime)
        total = sum(p.stat().st_size for p in files)
        for path in files:
            if (not path.stem.startswith("library_")
                    and path.stem.split("_")[0] not in active) or total + reserve > MAX_CACHE:
                total -= path.stat().st_size
                path.unlink(missing_ok=True)
        with self.store.factory() as session:
            session.execute(delete(InboxSend).where(InboxSend.created_at < self.clock() - 86400))
            session.commit()
