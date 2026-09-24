from __future__ import annotations

import asyncio
import base64
import json
import re
import time
from urllib.parse import urlsplit
from uuid import uuid4

from cryptography.hazmat.primitives import serialization
from py_vapid import Vapid
from py_vapid.utils import b64urlencode
from pywebpush import WebPushException, webpush
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.db.tables import (
    InboxNotification,
    LibrarySource,
    WebPushDelivery,
    WebPushSubscription,
)

KEY = re.compile(r"^[A-Za-z0-9_-]+$")
MAX_ATTEMPTS = 3


def _decoded_key(value: object, expected: int) -> str:
    if not isinstance(value, str) or not KEY.fullmatch(value) or len(value) > 256:
        raise ValueError("invalid push key")
    try:
        decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, TypeError):
        raise ValueError("invalid push key") from None
    if len(decoded) != expected:
        raise ValueError("invalid push key")
    return value


def validate_subscription(value):
    if not isinstance(value, dict) or set(value) - {"endpoint", "expirationTime", "keys"}:
        raise ValueError("invalid subscription")
    endpoint = value.get("endpoint")
    keys = value.get("keys")
    if not isinstance(endpoint, str) or not 1 <= len(endpoint) <= 2048:
        raise ValueError("invalid endpoint")
    try:
        parsed = urlsplit(endpoint)
    except ValueError:
        raise ValueError("invalid endpoint") from None
    if (parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password
            or parsed.fragment):
        raise ValueError("invalid endpoint")
    if not isinstance(keys, dict) or set(keys) != {"p256dh", "auth"}:
        raise ValueError("invalid subscription keys")
    return {
        "endpoint": endpoint,
        "p256dh": _decoded_key(keys.get("p256dh"), 65),
        "auth": _decoded_key(keys.get("auth"), 16),
    }


class WebPushService:
    def __init__(self, factory, store, private_key="", subject="", clock=time.time, sender=webpush):
        self.factory = factory
        self.store = store
        self.private_key = private_key.strip()
        self.subject = subject.strip()
        self.clock = clock
        self.sender = sender
        self.public_key = ""
        if self.private_key:
            vapid = Vapid.from_string(self.private_key)
            raw = vapid.public_key.public_bytes(
                serialization.Encoding.X962,
                serialization.PublicFormat.UncompressedPoint,
            )
            self.public_key = b64urlencode(raw)
            if not self.subject.startswith(("mailto:", "https://")):
                raise ValueError("WEB_PUSH_VAPID_SUBJECT must be mailto: or https://")

    @property
    def configured(self):
        return bool(self.private_key and self.public_key)

    def subscribe(self, raw, device_label=""):
        if not self.configured:
            raise RuntimeError("web push is not configured")
        value = validate_subscription(raw)
        if not isinstance(device_label, str) or len(device_label) > 64:
            raise ValueError("invalid device label")
        now = self.clock()
        for _ in range(2):
            try:
                with self.factory() as session:
                    row = session.scalar(select(WebPushSubscription).where(
                        WebPushSubscription.endpoint == value["endpoint"]
                    ))
                    if row is None:
                        row = WebPushSubscription(
                            id=uuid4().hex, endpoint=value["endpoint"],
                            p256dh=value["p256dh"], auth=value["auth"],
                            device_label=device_label.strip(), created_at=now,
                            last_success_at=None, disabled_at=None,
                        )
                        session.add(row)
                    else:
                        row.p256dh = value["p256dh"]
                        row.auth = value["auth"]
                        row.device_label = device_label.strip()
                        row.disabled_at = None
                    session.commit()
                    return {"enabled": True}
            except IntegrityError:
                continue
        raise RuntimeError("subscription conflict")

    def unsubscribe(self, raw):
        value = validate_subscription(raw)
        with self.factory() as session:
            row = session.scalar(select(WebPushSubscription).where(
                WebPushSubscription.endpoint == value["endpoint"]
            ))
            if row is not None and row.disabled_at is None:
                row.disabled_at = self.clock()
                session.commit()
        return {"enabled": False}

    def _payload(self, event, badge):
        count = max(1, int(event.unread_count or 1))
        reason = {
            "direct_reply": "Ответ на ваше сообщение",
            "private_message": "Личное сообщение",
            "mention": "Упоминание",
            "library_message": "Новое в библиотеке",
            "telegram_code": "Новый код Telegram",
        }.get(event.trigger_reason, "Новое сообщение")
        subtitle = f"{count} новых сообщений" if count > 1 else reason
        preview = " ".join((event.preview or "").split())[:180]
        return json.dumps({
            "title": event.title[:100], "subtitle": subtitle,
            "body": f"Последнее: {preview}" if count > 1 else preview,
            "tag": f"conversation:{event.conversation_id}",
            "conversation_id": event.conversation_id,
            "url": f"/?conversation={event.conversation_id}",
            "badge": badge,
        }, ensure_ascii=False)

    async def deliver_pending(self):
        if not self.configured:
            return
        now = self.clock()
        active = {row.id: row for row in self.store.active()}
        badge = self.store.unread_total(active.values())
        with self.factory() as session:
            subscriptions = list(session.scalars(select(WebPushSubscription).where(
                WebPushSubscription.disabled_at.is_(None)
            )))
            recent = list(session.scalars(select(InboxNotification).where(
                InboxNotification.created_at >= now - 86400,
                InboxNotification.suppressed.is_(False),
            ).order_by(InboxNotification.id.desc()).limit(200)))
            latest_by_conversation = {}
            for event in reversed(recent):
                latest_by_conversation[event.conversation_id] = event
            events = list(latest_by_conversation.values())
            muted_ids = set(session.scalars(select(LibrarySource.id).where(
                LibrarySource.notifications_muted.is_(True)
            )))
        for subscription in subscriptions:
            for event in events:
                conversation = active.get(event.conversation_id)
                if (event.created_at < subscription.created_at or conversation is None
                        or event.peer_id in self.store.ignored()
                        or event.trigger_id <= conversation.last_seen_message_id
                        or conversation.library_source_id in muted_ids):
                    continue
                await self._deliver(subscription.id, event, self._payload(event, badge), now)

    async def _deliver(self, subscription_id, event, payload, now):
        with self.factory() as session:
            delivery = session.scalar(select(WebPushDelivery).where(
                WebPushDelivery.subscription_id == subscription_id,
                WebPushDelivery.notification_id == event.id,
            ))
            if delivery is not None and (
                delivery.completed_at is not None or delivery.next_attempt_at > now
            ):
                return
            subscription = session.get(WebPushSubscription, subscription_id)
            if subscription is None or subscription.disabled_at is not None:
                return
            if delivery is None:
                delivery = WebPushDelivery(
                    subscription_id=subscription_id, notification_id=event.id,
                    attempts=0, next_attempt_at=0, completed_at=None,
                )
                session.add(delivery)
            delivery.attempts += 1
            subscription_info = {
                "endpoint": subscription.endpoint,
                "keys": {"p256dh": subscription.p256dh, "auth": subscription.auth},
            }
            attempts = delivery.attempts
            session.commit()
        try:
            await asyncio.to_thread(
                self.sender, subscription_info=subscription_info, data=payload,
                vapid_private_key=self.private_key,
                vapid_claims={"sub": self.subject}, ttl=300, timeout=10,
            )
        except WebPushException as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            with self.factory() as session:
                subscription = session.get(WebPushSubscription, subscription_id)
                delivery = session.scalar(select(WebPushDelivery).where(
                    WebPushDelivery.subscription_id == subscription_id,
                    WebPushDelivery.notification_id == event.id,
                ))
                if status in {404, 410} and subscription is not None:
                    subscription.disabled_at = now
                    delivery.completed_at = now
                elif attempts >= MAX_ATTEMPTS:
                    delivery.completed_at = now
                else:
                    delivery.next_attempt_at = now + (15, 60, 300)[attempts - 1]
                session.commit()
        except Exception:
            with self.factory() as session:
                delivery = session.scalar(select(WebPushDelivery).where(
                    WebPushDelivery.subscription_id == subscription_id,
                    WebPushDelivery.notification_id == event.id,
                ))
                if attempts >= MAX_ATTEMPTS:
                    delivery.completed_at = now
                else:
                    delivery.next_attempt_at = now + (15, 60, 300)[attempts - 1]
                session.commit()
        else:
            with self.factory() as session:
                subscription = session.get(WebPushSubscription, subscription_id)
                delivery = session.scalar(select(WebPushDelivery).where(
                    WebPushDelivery.subscription_id == subscription_id,
                    WebPushDelivery.notification_id == event.id,
                ))
                subscription.last_success_at = now
                delivery.completed_at = now
                session.commit()
