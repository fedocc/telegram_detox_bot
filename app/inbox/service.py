from __future__ import annotations

import asyncio
import io
import time
import warnings
from collections import OrderedDict
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError
from sqlalchemy import delete
from telethon.errors import RPCError
from telethon.tl.types import PeerChannel

from app.db.tables import InboxSend
from app.inbox.store import InboxStore
from app.services.attention import DETERMINISTIC_ALERT_TYPES, classify_incoming
from app.services.mentions import has_exact_fedocc_mention
from app.telegram.mapper import display_name

MAX_IMAGE = 10 * 1024 * 1024
MAX_MEDIA = 64 * 1024 * 1024
MAX_CACHE = 256 * 1024 * 1024


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
    def __init__(self, client, factory, ignored, cache_dir, self_id, clock=time.time):
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
        if trigger not in DETERMINISTIC_ALERT_TYPES:
            return
        chat = await event.get_chat()
        thread, forum = await thread_context(event.message, chat)
        self.store.activate(
            peer_id=event.chat_id, thread_id=thread, is_forum=forum,
            title=display_name(chat), trigger_id=event.id, preview=event.raw_text or "",
            reason=trigger,
        )

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

    async def send(self, key, request_id, text, image=None):
        async with self.action_lock:
            with self.store.factory() as session:
                previous = session.get(InboxSend, request_id)
                if previous:
                    if previous.conversation_id != key:
                        raise InboxError("Идентификатор отправки уже использован.", 409)
                    if previous.status == "sent":
                        return {"message_id": previous.message_id}
                    raise InboxError(
                        "Статус отправки неизвестен. Проверьте сообщения перед повтором.", 409,
                    )
            row = self.require(key)
            if not self.client.is_connected():
                raise InboxError("Telegram не подключён. Черновик сохранён.", 503)
            if not isinstance(text, str) or len(text.encode("utf-16-le")) // 2 > (
                1024 if image is not None else 4096
            ):
                raise InboxError("Слишком длинное сообщение (4096 символов; с фото — 1024).")
            if image is None and not text.strip():
                raise InboxError("Введите сообщение или прикрепите изображение.")
            photo = await asyncio.to_thread(normalize_image, image) if image is not None else None
            self.require(key)
            with self.store.factory() as session:
                session.add(InboxSend(request_id=request_id, conversation_id=key,
                                      created_at=self.clock(), status="pending"))
                session.commit()
            try:
                async with asyncio.timeout(90):
                    if photo is None:
                        sent = await self.client.send_message(
                            int(row.peer_id), text, reply_to=row.thread_id or None,
                            parse_mode=None, link_preview=False,
                        )
                    else:
                        sent = await self.client.send_file(
                            int(row.peer_id), photo, caption=text, reply_to=row.thread_id or None,
                            parse_mode=None, force_document=False,
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
            with self.store.factory() as session:
                record = session.get(InboxSend, request_id)
                record.status = "sent"
                record.message_id = sent.id
                session.commit()
            self.store.extend(key)
            if key in self.snapshots:
                self.snapshots[key]["fetched"] = 0
            return {"message_id": sent.id}

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

    async def cleanup(self, reserve=0):
        self.store.cleanup()
        for partial in self.cache_dir.glob("*.part"):
            partial.unlink(missing_ok=True)
        active = {row.id for row in self.store.active()}
        for key in list(self.snapshots):
            if key not in active:
                del self.snapshots[key]
        files = sorted(self.cache_dir.glob("*.bin"), key=lambda p: p.stat().st_mtime)
        total = sum(p.stat().st_size for p in files)
        for path in files:
            if path.stem.split("_")[0] not in active or total + reserve > MAX_CACHE:
                total -= path.stat().st_size
                path.unlink(missing_ok=True)
        with self.store.factory() as session:
            session.execute(delete(InboxSend).where(InboxSend.created_at < self.clock() - 86400))
            session.commit()
