"""Explicit message actions and contact cards; none of these grants chat access."""
from __future__ import annotations

import hashlib
import inspect
import json
import secrets
from collections import OrderedDict
from copy import copy
from datetime import UTC, datetime
from types import SimpleNamespace

from sqlalchemy import select
from telethon import functions, types, utils
from telethon.errors import FloodWaitError, RPCError

from app.db.tables import MorningDigest
from app.inbox.content import poll_content, poll_option_id
from app.inbox.errors import InboxError
from app.telegram.mapper import display_name


class InteractionMixin:
    async def complete_poll(self, target, message, *, fresh=False):
        media = getattr(message, "media", None)
        if not isinstance(media, types.MessageMediaPoll) or not media.results.min:
            return
        poll_id = str(media.poll.id)
        cached = self.full_poll_results.get(poll_id)
        if cached and cached[0] > self.clock() and not fresh:
            message.media = copy(media)
            message.media.results = cached[2]
            return
        kwargs = {"msg_id": message.id}
        # poll_hash was added in newer Telegram layers. Zero requests a full reply.
        if "poll_hash" in inspect.signature(functions.messages.GetPollResultsRequest).parameters:
            kwargs["poll_hash"] = 0
        try:
            response = await self._peer_call(target, lambda peer: self.client(
                functions.messages.GetPollResultsRequest(peer=peer, **kwargs)))
        except InboxError:
            if fresh:
                raise
            return
        except (RPCError, OSError, TimeoutError):
            if fresh:
                raise InboxError("Не удалось обновить опрос. Повторите позже.", 503) from None
            return  # Keep the question and options visible during transient failures.
        updates = getattr(response, "updates", None) or [getattr(response, "update", None)]
        update = next((item for item in updates if isinstance(item, types.UpdateMessagePoll)
                       and str(item.poll_id) == poll_id), None)
        if update is not None and not update.results.min:
            message.media = copy(media)
            message.media.poll = update.poll or media.poll
            message.media.results = update.results
            self.full_poll_results[poll_id] = (self.clock() + 15,
                                               message.media.poll, message.media.results)
            self.full_poll_results.move_to_end(poll_id)
            while len(self.full_poll_results) > 200:
                self.full_poll_results.popitem(last=False)

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

    async def vote_poll(self, scope, key, message_id, poll_id, options):
        if (not isinstance(poll_id, str) or not 1 <= len(poll_id) <= 24
                or not isinstance(options, list) or len(options) > 100
                or any(not isinstance(item, str) or len(item) > 256 for item in options)
                or len(set(options)) != len(options)):
            raise InboxError("Выберите варианты опроса.")
        async with self.action_lock:
            self._check_telegram_cooldown()
            self.interaction_target(scope, key)
            if not self.client.is_connected():
                raise InboxError("Нет соединения с Telegram. Повторите позже.", 503)
            target, message = await self.interaction_message(scope, key, message_id)
            media = getattr(message, "media", None)
            if not isinstance(media, types.MessageMediaPoll) or str(media.poll.id) != poll_id:
                raise InboxError("Опрос изменился. Обновите сообщение.", 409)
            await self.complete_poll(target, message, fresh=True)
            media = message.media
            content = poll_content(media)
            if content["closed"]:
                raise InboxError("Опрос уже завершён.", 422)
            answers = {poll_option_id(answer.option): answer.option
                       for answer in media.poll.answers}
            if any(item not in answers for item in options):
                raise InboxError("Такого варианта в опросе нет.", 422)
            if not content["multiple"] and len(options) > 1:
                raise InboxError("В этом опросе можно выбрать только один вариант.", 422)
            current = {item["id"] for item in content["options"] if item["chosen"]}
            if current == set(options):
                return {"ok": True, "content": content}
            if current and not content["can_revote"]:
                raise InboxError("В этом опросе нельзя изменить или отменить ответ.", 422)
            selected = [answers[item] for item in options]

            async def send(peer):
                # Peer resolution can await the network; recheck access immediately before sending.
                self.interaction_target(scope, key)
                return await self.client(functions.messages.SendVoteRequest(
                    peer=peer, msg_id=message_id, options=selected,
                ))

            try:
                response = await self._peer_call(target, send)
            except RPCError as exc:
                messages = {
                    "MESSAGE_POLL_CLOSED": "Опрос уже завершён.",
                    "REVOTE_NOT_ALLOWED": "В этом опросе нельзя изменить или отменить ответ.",
                    "POLL_MEMBER_RESTRICTED": "В этом опросе могут голосовать только подписчики.",
                    "POLL_COUNTRY_RESTRICTED": "Telegram ограничил голосование в этом опросе.",
                    "OPTION_INVALID": "Варианты опроса изменились. Обновите сообщение.",
                }
                raise InboxError(messages.get(exc.message,
                    "Telegram не разрешил голосование. Обновите опрос и повторите."), 422) from None

            # SendVote returns the authoritative result update. Some updates are `min`
            # and omit our chosen flags; the accepted request supplies those flags.
            updates = getattr(response, "updates", None) or [getattr(response, "update", None)]
            update = next((item for item in updates if isinstance(item, types.UpdateMessagePoll)
                           and str(item.poll_id) == poll_id), None)
            updated = copy(media)
            if update is not None and update.poll is not None:
                updated.poll = update.poll
            updated.results = copy(update.results) if update is not None else types.PollResults()
            counts = {item.option: item for item in (updated.results.results or ())}
            updated.results.results = []
            for answer in updated.poll.answers:
                count = copy(counts[answer.option]) if answer.option in counts else \
                    types.PollAnswerVoters(answer.option)
                count.chosen = answer.option in selected
                updated.results.results.append(count)
            # Invalidate every cached view of this poll, including an Inbox projection
            # when the vote was sent from Library. This changes no access state.
            self.invalidate_poll(poll_id, media=updated)
            self.interaction_target(scope, key)
            return {"ok": True, "content": poll_content(updated)}

    def invalidate_poll(self, poll_id, *, media=None, update=None):
        poll_id = str(poll_id)
        self.full_poll_results.pop(poll_id, None)
        for cache in (self.snapshots, self.library_snapshots):
            for key, snapshot in list(cache.items()):
                matching = [item for item in snapshot["messages"].values()
                            if isinstance(getattr(item, "media", None), types.MessageMediaPoll)
                            and str(item.media.poll.id) == poll_id]
                if not matching:
                    continue
                if media is None and update is None:
                    cache.pop(key, None)
                    continue
                for item in matching:
                    item.media = copy(media or item.media)
                    if update is not None:
                        item.media.poll = update.poll or item.media.poll
                        item.media.results = update.results
                # Keep already loaded history and pagination; only rebuild its payload.
                snapshot["fetched"] = 0
        for key, snapshot in list(self.pin_snapshots.items()):
            if any((item.get("content") or {}).get("id") == poll_id
                   for item in snapshot["payload"].get("pins", ())):
                self.pin_snapshots.pop(key, None)

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
            rows.append({"token": token, "photo_url": f"/api/contacts/{token}/photo",
                         **self.contact_card(user)})
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
        card = await self._profile_entity(user)
        card["photo_url"] = f"/api/contacts/{token}/photo"
        return card

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
        suffix = f"?message={message_id}" if message_id is not None else ""
        result["photo_url"] = f"/api/{scope}/{key}/photo{suffix}"
        return result

    async def profile_photo(self, scope, key, message_id=None):
        target = self.interaction_target(scope, key)
        if message_id is not None:
            _, message = await self.interaction_message(scope, key, message_id)
            entity = getattr(message, "sender", None) or await message.get_sender()
            if entity is None or str(utils.get_peer_id(entity)) in self.store.ignored():
                raise InboxError("Фотография недоступна.", 404)
            identity = str(utils.get_peer_id(entity))
            async def get_entity():
                return await self.client.get_entity(entity)
        else:
            identity = str(self.self_id if str(target.peer_id) == "me" else target.peer_id)
            async def get_entity():
                return await self._peer_call(target, self.client.get_entity)
        self._check_telegram_cooldown()
        try:
            result = await self.avatars.get(identity, get_entity)
        except FloodWaitError as exc:
            raise self._flood_wait_error(exc) from None
        self.interaction_target(scope, key)
        return result

    async def contact_photo(self, token):
        item = getattr(self, "contact_tokens", {}).get(token)
        if not item or item[1] <= self.clock():
            raise InboxError("Обновите список контактов.", 404)
        user = item[0]
        if str(user.id) in self.store.ignored():
            raise InboxError("Контакт недоступен.", 404)
        self._check_telegram_cooldown()
        try:
            result = await self.avatars.get(str(user.id), lambda: self.client.get_entity(user))
        except FloodWaitError as exc:
            raise self._flood_wait_error(exc) from None
        if item[1] <= self.clock() or str(user.id) in self.store.ignored():
            raise InboxError("Контакт недоступен.", 404)
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
        from app.inbox.content import message_content
        from app.inbox.service import MAX_MEDIA, rich_text_segments

        source, message = await self.digest_message(source_id, message_id)
        await self.complete_poll(source, message)
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
            "content": message_content(message),
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
