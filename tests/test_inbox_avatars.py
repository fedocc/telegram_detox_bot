import asyncio
import io
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp.test_utils import TestClient, TestServer
from PIL import Image

from app.inbox.avatars import MAX_AVATAR_DOWNLOAD, BoundedPhoto
from app.inbox.errors import InboxError
from app.inbox.web import create_app
from tests.test_inbox import activate
from tests.test_inbox_interactions import ORIGIN
from tests.test_inbox_interactions import feature_service as feature_service


def photo_client(service):
    raw = io.BytesIO()
    Image.new("RGB", (640, 640), "navy").save(raw, "JPEG", exif=b"private metadata")
    service.client.user.photo = SimpleNamespace(photo_id=100)

    async def download(entity, *, file, download_big):
        assert download_big
        file.write(raw.getvalue())
        return file
    service.client.download_profile_photo = AsyncMock(side_effect=download)


async def test_current_photo_is_bounded_cached_and_replaced_without_history(feature_service):
    service = feature_service
    photo_client(service)
    row = activate(service, peer="42", opened=False)
    first, second = await asyncio.gather(
        service.profile_photo("conversations", row.id),
        service.profile_photo("conversations", row.id))
    assert first == second
    service.client.download_profile_photo.assert_awaited_once()
    with Image.open(io.BytesIO(first)) as image:
        assert image.size == (512, 512)
        assert not image.getexif()
    stored = service.store.get_any(row.id)
    assert stored.opened_at is None and stored.expires_at == row.expires_at
    assert service.client.reads == []
    service.test_clock[0] += 301
    await service.profile_photo("conversations", row.id)
    assert service.client.download_profile_photo.await_count == 1
    service.client.user.photo.photo_id = 101
    service.test_clock[0] += 301
    await service.profile_photo("conversations", row.id)
    assert service.client.download_profile_photo.await_count == 2
    files = list(service.avatars.directory.iterdir())
    assert len(files) == 1 and files[0].suffix == ".jpg"
    assert files[0].stat().st_mode & 0o777 == 0o600
    service.client.user.photo = None
    service.test_clock[0] += 301
    assert await service.profile_photo("conversations", row.id) is None
    assert not list(service.avatars.directory.iterdir())


async def test_expired_chat_and_contact_cannot_serve_even_cached_photo(feature_service):
    service = feature_service
    photo_client(service)
    row = activate(service, peer="42")
    await service.profile_photo("conversations", row.id)
    service.test_clock[0] = row.expires_at + 1
    with pytest.raises(InboxError):
        await service.profile_photo("conversations", row.id)
    contact = (await service.contacts())["contacts"][0]
    assert await service.contact_photo(contact["token"])
    service.test_clock[0] += 601
    with pytest.raises(InboxError):
        await service.contact_photo(contact["token"])
    assert service.manual_open_status()["remaining"] == 2


async def test_photo_http_is_private_and_scoped(feature_service):
    service = feature_service
    photo_client(service)
    row = activate(service, peer="42", opened=False)
    server = TestServer(create_app(service))
    async with TestClient(server, headers={"Host": "127.0.0.1:8787"}) as client:
        path = f"/api/conversations/{row.id}/photo"
        response = await client.get(path)
        assert response.status == 200
        assert response.content_type == "image/jpeg"
        assert response.headers["Cache-Control"] == "no-store"
        assert (await response.read()).startswith(b"\xff\xd8")
        assert (await client.get(path, headers={"Origin": "https://example.org"})).status == 403
        assert (await client.get("/api/conversations/unknown/photo")).status == 404
        assert (await client.get(path + "?message=-1")).status == 400
        card = await (await client.get(f"/api/conversations/{row.id}/profile")).json()
        assert card["photo_url"] == path
        contacts = await (await client.get("/api/contacts")).json()
        contact_photo = contacts["contacts"][0]["photo_url"]
        assert (await client.get(contact_photo)).status == 200
        assert service.store.get_any(row.id).opened_at is None
        assert ORIGIN not in card["photo_url"]


def test_avatar_download_cannot_grow_without_bound():
    buffer = BoundedPhoto()
    with pytest.raises(InboxError) as error:
        buffer.write(b"x" * (MAX_AVATAR_DOWNLOAD + 1))
    assert error.value.status == 413
