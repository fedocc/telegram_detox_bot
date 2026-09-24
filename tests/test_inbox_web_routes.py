from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from aiohttp import FormData
from aiohttp.test_utils import TestClient, TestServer

from app.db.session import init_db
from app.db.tables import MorningDigest
from app.inbox.service import InboxError, InboxService
from app.inbox.web import create_app

SOURCE_ID = "0" + "a" * 31
OTHER_SOURCE_ID = "1" + "b" * 31
CONVERSATION_ID = "c" * 32
ORIGIN = "http://127.0.0.1:8787"


class RouteTelegram:
    def __init__(self):
        self.sent_files = []

    def is_connected(self):
        return True

    async def send_file(self, peer, attachment, **kwargs):
        path = Path(attachment)
        self.sent_files.append({
            "peer": peer,
            "body": path.read_bytes(),
            "path": path,
            "kwargs": kwargs,
        })
        return SimpleNamespace(id=901)

    async def send_message(self, peer, text, **kwargs):
        return SimpleNamespace(id=902)

    async def get_messages(self, peer, **kwargs):
        return []


@pytest.fixture()
def route_service(settings, tmp_path):
    client = RouteTelegram()
    service = InboxService(
        client,
        init_db(settings),
        lambda: set(),
        tmp_path / "media-cache",
        self_id=1,
    )
    service.store.upsert_library_source(
        source_id=SOURCE_ID,
        peer_type="user",
        peer_id="42",
        access_hash=4242,
        display_title="Writable bot",
        is_bot=True,
        library_enabled=True,
        allow_bot_write=True,
    )
    return service


async def csrf_headers(client):
    response = await client.get("/api/session")
    assert response.status == 200
    return {
        "Origin": ORIGIN,
        "X-Inbox-CSRF": (await response.json())["csrf"],
    }


async def test_badge_and_digest_history_are_read_only_server_state(route_service):
    row = route_service.store.activate(
        peer_id="42", peer_type="user", thread_id=0, is_forum=False,
        title="Nikita", trigger_id=9, preview="hello", reason="private_message",
    )
    end = datetime(2026, 9, 20, 4, tzinfo=UTC).replace(tzinfo=None)
    with route_service.store.factory() as session:
        session.add(MorningDigest(
            period_start=datetime(2026, 9, 19, 4), period_end=end, generated_at=end,
            model="gemini-3.8-flash", prompt_version="morning-v1",
            payload_json=json.dumps({"title": "Главное", "items": []}), status="success",
        ))
        session.commit()
    route_service.clock = lambda: datetime(2026, 9, 20, 12, tzinfo=UTC).timestamp()

    async with TestClient(
        TestServer(create_app(route_service)), headers={"Host": "127.0.0.1:8787"},
    ) as client:
        assert await (await client.get("/api/badge")).json() == {"badge": 1}
        listing = await (await client.get("/api/conversations")).json()
        assert listing["badge"] == 1
        history = await (await client.get("/api/digest/history")).json()
        assert history["digests"][0]["title"] == "Главное"
        headers = await csrf_headers(client)
        opened = await client.post(
            f"/api/conversations/{row.id}/open", json={}, headers=headers,
        )
        assert opened.status == 200
        assert (await opened.json())["conversation"]["unread_count"] == 0
        assert await (await client.get("/api/badge")).json() == {"badge": 0}


async def test_library_management_mutations_require_csrf_and_accept_digit_uuid(
        route_service):
    async with TestClient(
        TestServer(create_app(route_service, library_management_enabled=True)),
        headers={"Host": "127.0.0.1:8787"},
    ) as client:
        preference = {"source_id": SOURCE_ID, "notifications_muted": True}
        assert (await client.post(
            "/api/library/preferences", json=preference,
        )).status == 403

        headers = await csrf_headers(client)
        response = await client.post(
            "/api/library/preferences", json=preference, headers=headers,
        )
        assert response.status == 200
        assert (await response.json())["notifications_muted"] is True
        assert response.headers["Cache-Control"] == "no-store"

        response = await client.post(
            "/api/library/reorder",
            json={"source_ids": [SOURCE_ID]},
            headers=headers,
        )
        assert response.status == 200
        assert [row["id"] for row in (await response.json())["sources"]] == [
            "saved", SOURCE_ID,
        ]

        assert (await client.get(
            f"/api/library/{SOURCE_ID}/open",
        )).status == 405
        response = await client.post(
            f"/api/library/{SOURCE_ID}/open", json={}, headers=headers,
        )
        assert response.status == 200
        assert (await response.json())["source"]["id"] == SOURCE_ID

        injected = await client.post(
            "/api/library/preferences",
            json={"source_id": SOURCE_ID, "peer_id": "999"},
            headers=headers,
        )
        assert injected.status == 400
        ambiguous = await client.post(
            "/api/library/preferences",
            json={"source_id": SOURCE_ID, "token": "stale-token"},
            headers=headers,
        )
        assert ambiguous.status == 400


async def test_library_management_lock_preserves_selected_sources(route_service):
    async with TestClient(
        TestServer(create_app(route_service)),
        headers={"Host": "127.0.0.1:8787"},
    ) as client:
        assert (await client.get("/api/library/dialogs")).status == 404
        headers = await csrf_headers(client)
        assert (await client.post(
            "/api/library/preferences",
            json={"source_id": SOURCE_ID, "notifications_muted": True},
            headers=headers,
        )).status == 404
        assert (await client.post(
            "/api/library/reorder", json={"source_ids": [SOURCE_ID]}, headers=headers,
        )).status == 404
        response = await client.get("/api/library")
        assert response.status == 200
        assert [row["id"] for row in (await response.json())["sources"]] == [
            "saved", SOURCE_ID,
        ]


async def test_manual_open_api_consumes_only_successes_and_rejects_third_use(route_service):
    dialog_key = "route-manual-token"
    route_service.dialog_tokens[dialog_key] = {
        "purpose": "manual_open", "peer_type": "user", "peer_id": "42",
        "access_hash": 4242, "display_title": "Writable bot", "is_bot": True,
        "can_write": True, "source_id": SOURCE_ID,
        "expires": route_service.clock() + 300,
    }
    async with TestClient(
        TestServer(create_app(route_service)), headers={"Host": "127.0.0.1:8787"},
    ) as client:
        initial = await (await client.get("/api/manual-open/status")).json()
        assert initial["quota"]["used"] == 0
        assert (await client.post(
            "/api/manual-open", json={"token": dialog_key},
        )).status == 403
        assert (await (await client.get("/api/manual-open/status")).json())["quota"]["used"] == 0

        headers = await csrf_headers(client)
        first = await client.post("/api/manual-open", json={"token": dialog_key}, headers=headers)
        assert first.status == 200
        assert (await first.json())["quota"]["remaining"] == 1
        second = await client.post("/api/manual-open", json={"token": dialog_key}, headers=headers)
        assert second.status == 200
        assert (await second.json())["quota"]["remaining"] == 0
        third = await client.post("/api/manual-open", json={"token": dialog_key}, headers=headers)
        assert third.status == 429


async def test_history_pins_exact_and_search_routes_forward_validated_arguments(
        route_service, monkeypatch):
    library_history = AsyncMock(return_value={"messages": [], "next_before": None})
    library_pins = AsyncMock(return_value={"pins": [{"id": 7}]})
    library_message = AsyncMock(return_value={"message": {"id": 41}})
    library_search = AsyncMock(return_value={"results": [{"id": 39}]})
    conversation_pins = AsyncMock(return_value={"pins": [{"id": 8}]})
    conversation_message = AsyncMock(return_value={"message": {"id": 51}})
    conversation_search = AsyncMock(return_value={"results": [{"id": 49}]})
    monkeypatch.setattr(route_service, "library_history", library_history)
    monkeypatch.setattr(route_service, "library_pins", library_pins)
    monkeypatch.setattr(route_service, "library_message", library_message)
    monkeypatch.setattr(route_service, "library_search", library_search)
    monkeypatch.setattr(route_service, "conversation_pins", conversation_pins)
    monkeypatch.setattr(route_service, "conversation_message", conversation_message)
    monkeypatch.setattr(route_service, "conversation_search", conversation_search)

    async with TestClient(
        TestServer(create_app(route_service)),
        headers={"Host": "127.0.0.1:8787"},
    ) as client:
        assert (await client.get(
            f"/api/library/{SOURCE_ID}/messages?before=97",
        )).status == 200
        assert (await client.get(
            f"/api/library/{SOURCE_ID}/pins",
        )).status == 200
        assert (await client.get(
            f"/api/library/{SOURCE_ID}/messages/41",
        )).status == 200
        assert (await client.get(
            f"/api/library/{SOURCE_ID}/search?q=needle&before=40",
        )).status == 200

        assert (await client.get(
            f"/api/conversations/{CONVERSATION_ID}/pins",
        )).status == 200
        assert (await client.get(
            f"/api/conversations/{CONVERSATION_ID}/messages/51",
        )).status == 200
        assert (await client.get(
            f"/api/conversations/{CONVERSATION_ID}/search?q=reply&before=50",
        )).status == 200

        assert (await client.get(
            f"/api/library/{SOURCE_ID}/messages?before=0",
        )).status == 400
        assert (await client.get(
            f"/api/library/{SOURCE_ID}/messages/0",
        )).status == 404

    library_history.assert_awaited_once_with(SOURCE_ID, 97)
    library_pins.assert_awaited_once_with(SOURCE_ID)
    library_message.assert_awaited_once_with(SOURCE_ID, 41)
    library_search.assert_awaited_once_with(SOURCE_ID, "needle", 40)
    conversation_pins.assert_awaited_once_with(CONVERSATION_ID)
    conversation_message.assert_awaited_once_with(CONVERSATION_ID, 51)
    conversation_search.assert_awaited_once_with(CONVERSATION_ID, "reply", 50)


async def test_generic_bot_multipart_send_and_disabled_source_authorization(route_service):
    async with TestClient(
        TestServer(create_app(route_service, library_management_enabled=True)),
        headers={"Host": "127.0.0.1:8787"},
    ) as client:
        headers = await csrf_headers(client)
        form = FormData()
        form.add_field("request_id", str(uuid4()))
        form.add_field("text", "attached report")
        form.add_field(
            "file", b"route payload", filename="../../report.txt",
            content_type="text/plain",
        )
        response = await client.post(
            f"/api/library/{SOURCE_ID}/send", data=form, headers=headers,
        )
        assert response.status == 200
        assert await response.json() == {"message_id": 901}
        assert len(route_service.client.sent_files) == 1
        sent = route_service.client.sent_files[0]
        assert sent["peer"] == 42
        assert sent["body"] == b"route payload"
        assert sent["kwargs"]["caption"] == "attached report"
        assert sent["kwargs"]["force_document"] is True
        assert sent["kwargs"]["attributes"][0].file_name == "report.txt"
        assert not sent["path"].exists()
        assert not list(route_service.upload_dir.glob("*.upload"))

        disabled = await client.post(
            "/api/library/preferences",
            json={"source_id": SOURCE_ID, "library_enabled": False},
            headers=headers,
        )
        assert disabled.status == 200

        blocked_form = FormData()
        blocked_form.add_field("request_id", str(uuid4()))
        blocked_form.add_field("text", "must not send")
        blocked_form.add_field(
            "file", b"blocked", filename="blocked.txt", content_type="text/plain",
        )
        blocked = await client.post(
            f"/api/library/{SOURCE_ID}/send", data=blocked_form, headers=headers,
        )
        assert blocked.status == 404
        assert len(route_service.client.sent_files) == 1
        assert (await client.get(
            f"/api/library/{SOURCE_ID}/messages",
        )).status == 404
        assert not list(route_service.upload_dir.glob("*.upload"))


async def test_inbox_error_retry_after_is_preserved_by_api_middleware(
        route_service, monkeypatch):
    async def rate_limited(source_id):
        assert source_id == OTHER_SOURCE_ID
        raise InboxError("Повторите позже.", 429, retry_after=23)

    monkeypatch.setattr(route_service, "library_pins", rate_limited)
    async with TestClient(
        TestServer(create_app(route_service)),
        headers={"Host": "127.0.0.1:8787"},
    ) as client:
        response = await client.get(f"/api/library/{OTHER_SOURCE_ID}/pins")
        assert response.status == 429
        assert response.headers["Retry-After"] == "23"
        assert response.headers["Cache-Control"] == "no-store"
        assert await response.json() == {"error": "Повторите позже."}
