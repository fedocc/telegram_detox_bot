"""Local visual fixture. No settings, credentials or Telegram session are loaded.

Run: python -m app.inbox.demo [--empty]
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from PIL import Image, ImageDraw

from app.config import Settings
from app.db.session import init_db
from app.inbox.service import InboxService
from app.inbox.web import serve_inbox


class DemoClient:
    def is_connected(self):
        return True


async def main():
    with tempfile.TemporaryDirectory(prefix="inbox-demo-") as directory:
        root = Path(directory)
        settings = Settings(_env_file=None, database_url=f"sqlite:///{root / 'demo.db'}")
        service = InboxService(DemoClient(), init_db(settings), lambda: set(), root / "cache", 1)
        now = service.clock()
        if "--empty" not in sys.argv:
            rows = []
            for index, (title, preview, age) in enumerate([
                ("Flare Team", "Никита: @fedocc глянь логи…", 0),
                ("Core Backend", "Елена: @fedocc нужен апрув на релиз", 1),
                ("Design System", "Павел: @fedocc обнови токен темы", 2),
            ]):
                row = service.store.activate(peer_id=str(-100123-index), thread_id=42,
                    is_forum=True, title=title, trigger_id=3, preview=preview)
                with service.store.factory() as session:
                    from app.db.tables import InboxConversation

                    record = session.get(InboxConversation, row.id)
                    record.activated_at = now - age * 60
                    record.opened_at = None
                    record.expires_at = 0
                    record.topic_title = "#backend-infra" if index == 0 else "Обсуждение"
                    session.commit()
                rows.append(service.store.get(row.id))
            photo = root / "sample.jpg"
            picture = Image.new("RGB", (560, 320), "#141b21")
            draw = ImageDraw.Draw(picture)
            draw.rectangle((25, 25, 535, 295), fill="#202b36", outline="#44474b", width=2)
            for i in range(12):
                draw.rectangle((45, 48 + i*18, 180+(i%4)*65, 52+i*18),
                               fill="#799298" if i % 3 else "#bca57e")
            picture.save(photo)
            messages = []
            texts = ["Пулл-реквест готов к деплою", "", "@fedocc что с интеграцией? API возвращает "
                "401 на вебхуках авторизации, проверь конфиг в волте.", "", "",
                "Понял, сейчас перезалью сертификаты и отпишусь.", "", "", "", ""]
            for index, text in enumerate(texts, 1):
                media = None
                if index in {2, 4, 5, 7, 8, 9, 10}:
                    kind = {2:"voice", 4:"photo", 5:"file", 7:"video",
                            8:"voice", 9:"audio", 10:"video_note"}[index]
                    media = {"kind":kind, "name":"crash_dump_0912.log" if index==5 else kind,
                        "size":24576, "duration":42, "available":True,
                        "url":f"/api/conversations/{rows[0].id}/media/{index}"}
                messages.append({"id":index, "text":text, "own":index==6,
                    "sender":"Никита Тарасов" if index==3 else "Алексей Романов",
                    "timestamp":datetime.now(UTC).replace(hour=11, minute=19+index).isoformat(),
                    "mention":index==3, "media":media,
                    "reply":{"sender":"Алексей Романов", "text":
                        "Подготовил ветку fix/auth-signature для стейджа"} if index==1 else None})

            async def history(key):
                from app.inbox.service import conversation_json

                return {"conversation": conversation_json(service.require(key)), "messages": [
                    {**m, "media": {**m["media"], "url":
                        f"/api/conversations/{key}/media/{m['id']}"} if m["media"] else None}
                    for m in messages
                ]}

            async def media(key, message_id):
                service.require(key)
                if message_id in {7, 10} and '--video' in sys.argv:
                    video = Path(sys.argv[sys.argv.index('--video') + 1])
                    return video, 'video/webm', 'sample.webm', True
                if message_id == 4:
                    return photo, "image/jpeg", "sample.jpg", True
                fixture = root / ("sample.wav" if message_id in {2, 8, 9} else "sample.log")
                if message_id in {2, 8, 9}:
                    import wave
                    with wave.open(str(fixture), "wb") as audio:
                        audio.setnchannels(1)
                        audio.setsampwidth(2)
                        audio.setframerate(8000)
                        audio.writeframes(b"\0\0" * 8000 * 42)
                else:
                    fixture.write_text("Synthetic visual fixture\n")
                playable = message_id in {2, 8, 9}
                mime = "audio/wav" if playable else "application/octet-stream"
                return fixture, mime, fixture.name, playable

            async def send(key, request_id, text, image=None):
                from app.inbox.service import InboxError

                raise InboxError("Демо: отправка отключена. Черновик сохранён.", 422)

            service.history = history
            service.media = media
            service.send = send
        async with serve_inbox(service):
            print("Synthetic local preview: http://127.0.0.1:8787", flush=True)
            await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
