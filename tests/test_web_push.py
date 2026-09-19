from __future__ import annotations

import base64
import json
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import TestClient, TestServer
from py_vapid import Vapid
from py_vapid.utils import b64urlencode
from pywebpush import WebPushException
from sqlalchemy import func, select

from app.db.session import init_db
from app.db.tables import WebPushDelivery, WebPushSubscription
from app.inbox.push import WebPushService, validate_subscription
from app.inbox.service import InboxService
from app.inbox.store import InboxStore
from app.inbox.web import create_app


def vapid_private_key():
    vapid = Vapid()
    vapid.generate_keys()
    raw = vapid.private_key.private_numbers().private_value.to_bytes(32, "big")
    return b64urlencode(raw)


def encoded(size, first=b""):
    return base64.urlsafe_b64encode(first + b"x" * (size - len(first))).decode().rstrip("=")


def subscription(endpoint="https://push.example.test/device"):
    return {
        "endpoint": endpoint,
        "expirationTime": None,
        "keys": {"p256dh": encoded(65, b"\x04"), "auth": encoded(16)},
    }


@pytest.fixture()
def push_fixture(settings):
    factory = init_db(settings)
    ignored = set()
    clock = [1000.0]
    store = InboxStore(factory, lambda: ignored, clock=lambda: clock[0])
    sent = []

    def sender(**kwargs):
        sent.append(kwargs)
        return SimpleNamespace(status_code=201)

    push = WebPushService(
        factory, store, vapid_private_key(), "mailto:test@example.com",
        clock=lambda: clock[0], sender=sender,
    )
    return SimpleNamespace(factory=factory, store=store, push=push, sent=sent,
                           ignored=ignored, clock=clock)


def test_public_vapid_config_and_subscription_is_idempotent(push_fixture):
    push = push_fixture.push
    assert push.configured and len(push.public_key) > 80
    assert push.subscribe(subscription(), "iPhone") == {"enabled": True}
    assert push.subscribe(subscription(), "iPhone") == {"enabled": True}
    with push_fixture.factory() as session:
        assert session.scalar(select(func.count(WebPushSubscription.id))) == 1


def test_unsubscribe_is_idempotent(push_fixture):
    push_fixture.push.subscribe(subscription())
    assert push_fixture.push.unsubscribe(subscription()) == {"enabled": False}
    assert push_fixture.push.unsubscribe(subscription()) == {"enabled": False}


@pytest.mark.parametrize("value", [
    {},
    {"endpoint": "http://push.example.test/x", "keys": {}},
    {"endpoint": "https://user:pass@push.example.test/x",
     "keys": {"p256dh": encoded(65), "auth": encoded(16)}},
    {"endpoint": "https://push.example.test/x",
     "keys": {"p256dh": "bad!", "auth": encoded(16)}},
])
def test_invalid_subscription_rejected(value):
    with pytest.raises(ValueError):
        validate_subscription(value)


@pytest.mark.parametrize("reason", ["private_message", "mention_only", "direct_reply"])
async def test_attention_event_drives_one_grouped_push(push_fixture, reason):
    item = subscription()
    push_fixture.push.subscribe(item)
    push_fixture.clock[0] += 1
    row = push_fixture.store.activate(
        peer_id="2", peer_type="user", thread_id=0, is_forum=False,
        title="Ivan", trigger_id=10, preview="hello", reason=reason,
    )
    await push_fixture.push.deliver_pending()
    await push_fixture.push.deliver_pending()

    assert len(push_fixture.sent) == 1
    payload = json.loads(push_fixture.sent[0]["data"])
    assert payload["tag"] == f"conversation:{row.id}"
    assert payload["url"] == f"/?conversation={row.id}"
    assert payload["badge"] == 1
    with push_fixture.factory() as session:
        assert session.scalar(select(func.count(WebPushDelivery.id))) == 1


async def test_burst_uses_latest_preview_and_one_push_per_conversation(push_fixture):
    push_fixture.push.subscribe(subscription())
    for trigger in (10, 11, 12):
        push_fixture.clock[0] += 1
        push_fixture.store.activate(
            peer_id="2", peer_type="user", thread_id=0, is_forum=False,
            title="Ivan", trigger_id=trigger, preview=f"message {trigger}",
            reason="private_message",
        )
    await push_fixture.push.deliver_pending()
    assert len(push_fixture.sent) == 1
    payload = json.loads(push_fixture.sent[0]["data"])
    assert payload["subtitle"] == "3 новых сообщений"
    assert payload["body"] == "Последнее: message 12"


async def test_unmuted_library_attention_pushes(push_fixture):
    push_fixture.push.subscribe(subscription())
    source = push_fixture.store.upsert_library_source(
        source_id="course", peer_type="chat", peer_id="-22", access_hash=None,
        display_title="Course", is_bot=False, notifications_muted=False,
    )
    push_fixture.clock[0] += 1
    push_fixture.store.activate(
        peer_id="-22", peer_type="chat", thread_id=0, is_forum=False,
        title="Course", trigger_id=10, preview="announcement", reason="library_message",
        library_source_id=source.id,
    )
    await push_fixture.push.deliver_pending()
    assert len(push_fixture.sent) == 1
    assert json.loads(push_fixture.sent[0]["data"])["subtitle"] == "Новое в библиотеке"


async def test_muted_and_ignored_attention_never_push(push_fixture):
    push_fixture.push.subscribe(subscription())
    push_fixture.clock[0] += 1
    push_fixture.store.activate(
        peer_id="2", peer_type="user", thread_id=0, is_forum=False,
        title="Muted", trigger_id=10, preview="quiet", reason="private_message",
        notifications_muted=True,
    )
    push_fixture.store.activate(
        peer_id="3", peer_type="user", thread_id=0, is_forum=False,
        title="Ignored", trigger_id=11, preview="ignored", reason="private_message",
    )
    push_fixture.ignored.add("3")
    await push_fixture.push.deliver_pending()
    assert push_fixture.sent == []


@pytest.mark.parametrize("status", [404, 410])
async def test_gone_endpoint_is_disabled(push_fixture, status):
    def gone(**_kwargs):
        raise WebPushException("gone", response=SimpleNamespace(status_code=status))

    push_fixture.push.sender = gone
    push_fixture.push.subscribe(subscription())
    push_fixture.clock[0] += 1
    push_fixture.store.activate(
        peer_id="2", peer_type="user", thread_id=0, is_forum=False,
        title="Ivan", trigger_id=10, preview="hello", reason="private_message",
    )
    await push_fixture.push.deliver_pending()
    with push_fixture.factory() as session:
        row = session.scalar(select(WebPushSubscription))
        assert row.disabled_at == push_fixture.clock[0]


async def test_transient_failure_retries_only_three_times(push_fixture):
    def fail(**_kwargs):
        raise WebPushException("temporary", response=SimpleNamespace(status_code=503))

    push_fixture.push.sender = fail
    push_fixture.push.subscribe(subscription())
    push_fixture.clock[0] += 1
    push_fixture.store.activate(
        peer_id="2", peer_type="user", thread_id=0, is_forum=False,
        title="Ivan", trigger_id=10, preview="hello", reason="private_message",
    )
    for advance in (0, 15, 60, 300):
        push_fixture.clock[0] += advance
        await push_fixture.push.deliver_pending()
    with push_fixture.factory() as session:
        delivery = session.scalar(select(WebPushDelivery))
        assert delivery.attempts == 3 and delivery.completed_at is not None


async def test_push_api_config_subscribe_duplicate_unsubscribe_and_validation(settings, tmp_path):
    client = SimpleNamespace(is_connected=lambda: True)
    service = InboxService(
        client, init_db(settings), lambda: set(), tmp_path / "media", self_id=1,
        web_push_private_key=vapid_private_key(), web_push_subject="mailto:test@example.com",
    )
    async with TestClient(TestServer(create_app(service)),
                          headers={"Host": "127.0.0.1:8787"}) as browser:
        config = await (await browser.get("/api/push/config")).json()
        assert config["configured"] is True and len(config["public_key"]) > 80
        csrf = (await (await browser.get("/api/session")).json())["csrf"]
        headers = {"Origin": "http://127.0.0.1:8787", "X-Inbox-CSRF": csrf}
        body = {"subscription": subscription(), "device_label": "iPhone"}
        for _ in range(2):
            assert (await browser.post(
                "/api/push/subscriptions", json=body, headers=headers,
            )).status == 200
        assert (await browser.post(
            "/api/push/subscriptions", json={"subscription": {}}, headers=headers,
        )).status == 400
        assert (await browser.post(
            "/api/push/unsubscribe", json={"subscription": subscription()}, headers=headers,
        )).status == 200
