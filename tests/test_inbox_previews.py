from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.inbox.service import message_preview_text
from tests.test_inbox import Message, activate
from tests.test_inbox import service as service


@pytest.mark.parametrize("kind,label", [
    ("voice", "Голосовое сообщение"), ("video_note", "Кружок"),
    ("photo", "Фото"), ("video", "Видео"), ("audio", "Аудиофайл"),
    ("sticker", "Стикер"), ("gif", "GIF"), ("poll", "Опрос"),
    ("contact", "Контакт"), ("geo", "Геопозиция"),
])
async def test_media_has_nonempty_chat_preview_and_notification(service, kind, label):
    message = Message(100, "", **{kind: True})
    if kind == "video_note":
        message.video = True
    if kind == "voice":
        message.audio = True
    await service.observe(SimpleNamespace(chat_id=42, id=100, message=message,
        out=False, sender_id=2, raw_text="",
        get_chat=AsyncMock(return_value=SimpleNamespace(first_name="Тест", forum=False))),
        trigger="private_message")
    row = service.store.active()[0]
    assert row.preview == label
    assert service.store.notifications(after=0)["events"][0]["preview"] == label


def test_caption_filename_and_own_prefix_are_readable():
    assert message_preview_text(Message(1, "Подпись", photo=True)) == "Фото: Подпись"
    assert message_preview_text(Message(1, "", file=SimpleNamespace(name="отчёт.pdf"))) \
        == "Файл: отчёт.pdf"
    assert message_preview_text(Message(1, "Мой ответ"), own=True) == "Вы: Мой ответ"


async def test_send_updates_preview_without_changing_unread_or_deadline(service):
    row = activate(service)
    before = service.store.get(row.id)
    await service.send(row.id, str(uuid4()), "Мой ответ")
    after = service.store.get(row.id)
    assert after.preview == "Вы: Мой ответ"
    assert (after.unread_count, after.expires_at, after.trigger_id) == (
        before.unread_count, before.expires_at, before.trigger_id)
    assert service.store.notifications(after=0)["events"] == []


async def test_restart_preview_refresh_does_not_open_chats_or_notify(service):
    row = activate(service, opened=False)
    service.client.messages = [Message(200, "Ответ из Telegram", out=True)]
    await service.refresh_previews()
    after = service.store.get_any(row.id)
    assert after.preview == "Вы: Ответ из Telegram"
    assert after.opened_at is None and after.expires_at == 0
    assert len(service.client.reads) == 1
    assert service.client.reads[0][1]["limit"] == 1
    assert service.store.notifications(after=0)["events"] == []
    service.store.close(row.id)
    closed_preview = service.store.get_any(row.id).preview
    service.client.messages = [Message(201, "закрытый чат")]
    await service.refresh_previews()
    assert service.store.get_any(row.id).preview == closed_preview
    assert len(service.client.reads) == 1
