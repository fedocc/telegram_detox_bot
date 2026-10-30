"""Explicit message actions and contact cards; none of these grants chat access."""
from __future__ import annotations

import hashlib
import json
import secrets
from collections import OrderedDict
from datetime import UTC, datetime
from types import SimpleNamespace

from sqlalchemy import select
from telethon import functions, types, utils
from telethon.errors import FloodWaitError, RPCError

from app.db.tables import MorningDigest
from app.inbox.errors import InboxError
from app.telegram.mapper import display_name


class InteractionMixin:
    def interaction_target(self, scope, key):
        if scope == "conversations":
            return self.require(key)
        if scope == "library":
            return self.library_source(key)
        if scope == "quick-write":
            return self.manual_write_source(key, require_access=True)
        raise InboxError("Раздел не найден.", 404)

    async def interaction_message(self, scope, key, message_id):
        target = self.interaction_target(scope, key)
        message = await self._peer_call(
            target, lambda peer: self.client.get_messages(peer, ids=message_id),
        )
        if (not message or isinstance(message, types.MessageEmpty)
                or (scope == "conversations" and not await self.belongs(message, target))):
            raise InboxError("Сообщение недоступно.", 404)
        self.interaction_target(scope, key)
        return target, message

    async def available_reactions(self):
        cached = getattr(self, "reaction_catalog", None)
        if cached and cached[0] > self.clock():
            return {"reactions": cached[1]}
        self._check_telegram_cooldown()
        try:
            result = await self.client(functions.messages.GetAvailableReactionsRequest(hash=0))
        except FloodWaitError as exc:
            raise self._flood_wait_error(exc) from None
        except RPCError:
            if cached:
                return {"reactions": cached[1]}
            raise InboxError(
                "Не удалось загрузить реакции Telegram. Повторите позже.", 503,
            ) from None
        reactions = [{"emoji": item.reaction, "title": item.title}
                     for item in getattr(result, "reactions", ())
                     if not item.inactive and not item.premium]
        if not reactions:
            raise InboxError("Список реакций Telegram пока недоступен.", 503)
        self.reaction_catalog = (self.clock() + 3600, reactions)
        return {"reactions": reactions}

    async def set_reaction(self, scope, key, message_id, emoji):
        if not isinstance(emoji, str) or len(emoji) > 32:
            raise InboxError("Выберите стандартную реакцию.")
        async with self.action_lock:
            self._check_telegram_cooldown()
            target, _ = await self.interaction_message(scope, key, message_id)
            if emoji:
                catalog = (await self.available_reactions())["reactions"]
                emoji = next((item["emoji"] for item in catalog
                              if item["emoji"].replace("\ufe0f", "") ==
                              emoji.replace("\ufe0f", "")), None)
                if emoji is None:
                    raise InboxError("Выберите стандартную реакцию Telegram.")
            try:
                await self._peer_call(target, lambda peer: self.client(
                    functions.messages.SendReactionRequest(
                        peer=peer, msg_id=message_id,
                        reaction=[types.ReactionEmoji(emoji)] if emoji else [],
                    ),
                ))
            except FloodWaitError as exc:
                raise self._flood_wait_error(exc) from None
            except RPCError:
                raise InboxError("В этом чате такая реакция недоступна.", 422) from None
            # A reaction never starts or extends a conversation's countdown.
            self.interaction_target(scope, key)
            self.snapshots.pop(key, None)
            self.library_snapshots.clear()
            return {"ok": True}

    @staticmethod
    def contact_card(user):
        return {
            "title": display_name(user),
            "username": getattr(user, "username", None) or "",
            "phone": getattr(user, "phone", None) or "",
            "is_bot": bool(getattr(user, "bot", False)),
        }

    async def contacts(self):
        self._check_telegram_cooldown()
        try:
            result = await self.client(functions.contacts.GetContactsRequest(hash=0))
        except FloodWaitError as exc:
            raise self._flood_wait_error(exc) from None
        tokens = getattr(self, "contact_tokens", OrderedDict())
        now = self.clock()
        tokens = OrderedDict((token, value) for token, value in tokens.items()
                             if value[1] > now)
        rows = []
        for user in (getattr(result, "users", None) or ()):
            if str(user.id) in self.store.ignored() or getattr(user, "deleted", False):
                continue
            token = secrets.token_urlsafe(24)
            tokens[token] = (user, now + 600)
            rows.append({"token": token, **self.contact_card(user)})
        while len(tokens) > max(10000, len(rows)):
            tokens.popitem(last=False)
        self.contact_tokens = tokens
        return {"contacts": sorted(rows, key=lambda row: row["title"].casefold())}

    async def contact_profile(self, token):
        item = getattr(self, "contact_tokens", {}).get(token)
        if not item or item[1] <= self.clock():
            raise InboxError("Обновите список контактов.", 404)
        user = item[0]
        if str(user.id) in self.store.ignored():
            raise InboxError("Контакт недоступен.", 404)
        return await self._profile_entity(user)

    async def profile(self, scope, key, message_id=None):
        target = self.interaction_target(scope, key)
        if message_id is not None:
            _, message = await self.interaction_message(scope, key, message_id)
            entity = getattr(message, "sender", None) or await message.get_sender()
        else:
            entity = await self._peer_call(target, self.client.get_entity)
        if entity is None or str(utils.get_peer_id(entity)) in self.store.ignored():
            raise InboxError("Профиль недоступен.", 404)
        result = await self._profile_entity(entity)
        self.interaction_target(scope, key)
        return result

    async def _profile_entity(self, entity):
        self._check_telegram_cooldown()
        card = self.contact_card(entity)
        card.update(about="", birthday=None, members=None)
        try:
            if isinstance(entity, types.User):
                result = await self.client(functions.users.GetFullUserRequest(entity))
                full = result.full_user
                birthday = getattr(full, "birthday", None)
                if birthday:
                    card["birthday"] = {"day": birthday.day, "month": birthday.month,
                                        "year": getattr(birthday, "year", None)}
            elif isinstance(entity, types.Channel):
                result = await self.client(functions.channels.GetFullChannelRequest(entity))
                full = result.full_chat
            elif isinstance(entity, types.Chat):
                result = await self.client(functions.messages.GetFullChatRequest(entity.id))
                full = result.full_chat
            else:
                full = None
            card["about"] = getattr(full, "about", "") or ""
            card["members"] = getattr(full, "participants_count", None)
        except FloodWaitError as exc:
            raise self._flood_wait_error(exc) from None
        except RPCError:
            raise InboxError("Telegram временно не отдаёт профиль.", 503) from None
        return card

    async def digest_message(self, source_id, message_id):
        # Only exact references in an already published digest are capabilities.
        reference = None
        with self.store.factory() as session:
            payloads = session.scalars(select(MorningDigest.payload_json).where(
                MorningDigest.status == "success",
            ).order_by(MorningDigest.period_end.desc()).limit(31))
            for payload in payloads:
                for item in json.loads(payload).get("items", []):
                    for source in item.get("sources", []):
                        if (source.get("source_id") == source_id
                                and source.get("message_id") == message_id):
                            reference = source
                            break
                if reference:
                    break
        if reference is None:
            raise InboxError("В сводке нет такого сообщения.", 404)
        source = self.store.library_source(source_id, enabled_only=False)
        if source is not None and not source.library_enabled:
            raise InboxError("Источник отключён.", 404)
        if source is None:
            identity = reference.get("_peer")
            if identity:
                source = SimpleNamespace(**identity)
            else:
                # Compatibility for digests made before peer identity was persisted.
                self._check_telegram_cooldown()
                async for dialog in self.client.iter_dialogs(limit=1000):
                    entity = dialog.entity
                    peer = dialog.input_entity
                    kind, marked, access_hash = self._input_identity(peer, dialog.id, entity)
                    opaque = "d" + hashlib.sha256(f"{kind}:{marked}".encode()).hexdigest()[:20]
                    if opaque == source_id:
                        source = SimpleNamespace(peer_type=kind, peer_id=marked,
                            access_hash=access_hash, display_title=dialog.name)
                        break
        if source is None or str(source.peer_id) in self.store.ignored():
            raise InboxError("Источник сообщения недоступен.", 404)
        message = await self._peer_call(
            source, lambda peer: self.client.get_messages(peer, ids=message_id),
        )
        if not message or isinstance(message, types.MessageEmpty):
            raise InboxError("Сообщение удалено или недоступно.", 404)
        return source, message

    async def digest_preview(self, source_id, message_id):
        from app.inbox.service import MAX_MEDIA, rich_text_segments

        source, message = await self.digest_message(source_id, message_id)
        text = getattr(message, "raw_text", "") or ""
        file = getattr(message, "file", None)
        mime = getattr(file, "mime_type", "") or ""
        media = None
        if file:
            media = {"name": file.name or "Вложение", "size": file.size,
                "available": bool(file.size and file.size <= MAX_MEDIA),
                "kind": ("video" if mime.startswith("video/") else
                         "audio" if mime.startswith("audio/") else
                         "photo" if mime.startswith("image/") else "file"),
                "url": f"/api/digest/{source_id}/media/{message_id}"}
        return {"title": getattr(source, "display_title", "Источник сводки"), "message": {
            "id": message.id, "text": text,
            "segments": rich_text_segments(text, getattr(message, "entities", None)),
            "sender": display_name(message.sender) if message.sender else "",
            "timestamp": message.date.isoformat(), "own": bool(message.out),
            "mention": False, "reply": None, "media": media,
        }}

    async def quoted_reply(self, peer, reply_to, quote_text=None, quote_offset=None, row=None):
        target = await self.validate_reply(peer, reply_to, row)
        if quote_text is None and quote_offset is None:
            return target
        if (not target or not isinstance(quote_text, str) or not quote_text
                or isinstance(quote_offset, bool) or not isinstance(quote_offset, int)
                or quote_offset < 0 or len(quote_text.encode("utf-16-le")) // 2 > 1024):
            raise InboxError("Выделите фрагмент исходного сообщения для цитаты.", 422)
        parent = await self.client.get_messages(peer, ids=target)
        raw = (getattr(parent, "raw_text", "") or "").encode("utf-16-le")
        quoted = quote_text.encode("utf-16-le")
        if raw[quote_offset * 2:quote_offset * 2 + len(quoted)] != quoted:
            raise InboxError("Цитата изменилась. Выделите фрагмент ещё раз.", 422)
        return types.InputReplyToMessage(target, quote_text=quote_text,
            quote_offset=quote_offset, top_msg_id=(row.thread_id or None) if row else None)

    async def send_quote(self, peer, text, reply, *, attachment=None, photo=False,
                         mime_type=None, filename=None, schedule=None):
        # The high-level Telethon API discards InputReplyToMessage quote fields.
        peer = await self.client.get_input_entity(peer)
        options = {"peer": peer, "message": text, "reply_to": reply}
        if schedule:
            options["schedule_date"] = datetime.now(UTC) + schedule
        if attachment is None:
            request = functions.messages.SendMessageRequest(**options, no_webpage=True)
        else:
            uploaded = await self.client.upload_file(attachment)
            media = (types.InputMediaUploadedPhoto(uploaded) if photo else
                types.InputMediaUploadedDocument(uploaded,
                    mime_type=mime_type or "application/octet-stream",
                    attributes=self._file_attributes(filename) or []))
            request = functions.messages.SendMediaRequest(**options, media=media)
        result = await self.client(request)
        if isinstance(result, types.UpdateShortSentMessage):
            return SimpleNamespace(id=result.id)
        for update in getattr(result, "updates", ()):
            if isinstance(update, types.UpdateMessageID) and update.random_id == request.random_id:
                return SimpleNamespace(id=update.id)
        for update in getattr(result, "updates", ()):
            message = getattr(update, "message", None)
            if message is not None and getattr(message, "out", False):
                return SimpleNamespace(id=message.id)
        raise OSError("Delivery status unknown")
