from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import logging
import os
import secrets
from pathlib import Path
from urllib.parse import quote, unquote
from uuid import UUID

from aiohttp import web

from app.inbox.service import MAX_IMAGE, InboxError, conversation_json
from app.inbox.store import ACTIVE_MINUTES

HOST = "127.0.0.1"
PORT = 8787
STATIC = Path(__file__).parent / "static"
logger = logging.getLogger(__name__)


def create_app(service, *, port=PORT):
    csrf = secrets.token_urlsafe(32)
    expected_host = f"127.0.0.1:{port}"
    expected_origin = f"http://{expected_host}"

    @web.middleware
    async def security(request, handler):
        try:
            if request.host != expected_host:
                raise InboxError("Недопустимый Host.", 403)
            origin = request.headers.get("Origin")
            if origin and origin != expected_origin:
                raise InboxError("Недопустимый Origin.", 403)
            if request.headers.get("Sec-Fetch-Site") not in {None, "same-origin", "none"}:
                raise InboxError("Межсайтовый запрос запрещён.", 403)
            if request.method not in {"GET", "HEAD"}:
                if (origin != expected_origin or not secrets.compare_digest(
                        request.headers.get("X-Inbox-CSRF", ""), csrf)):
                    raise InboxError("Перезагрузите страницу перед отправкой.", 403)
                if request.content_type not in {"application/json", "multipart/form-data"}:
                    raise InboxError("Неподдерживаемый формат запроса.", 415)
            response = await handler(request)
        except InboxError as exc:
            response = web.json_response({"error": str(exc)}, status=exc.status)
        except web.HTTPException as exc:
            response = web.json_response({"error": "Запрос не поддерживается."}, status=exc.status)
        except (ValueError, TypeError, binascii.Error):
            response = web.json_response({"error": "Некорректный запрос."}, status=400)
        except Exception as exc:
            # Never log request bodies, Telegram objects or exception messages.
            logger.warning("Inbox request failed (%s)", type(exc).__name__)
            response = web.json_response({"error": "Не удалось загрузить данные Telegram. "
                "Попробуйте ещё раз; черновик сохранён."}, status=503)
        response.headers.update({
            "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer", "X-Frame-Options": "DENY",
            "Cross-Origin-Resource-Policy": "same-origin",
            "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; "
                "img-src 'self' blob:; media-src 'self' blob:; "
                "connect-src 'self' http://127.0.0.1:8788; "
                "base-uri 'none'; frame-ancestors 'none'; form-action 'none'",
        })
        return response

    async def index(request):
        return web.FileResponse(STATIC / "index.html")

    async def asset(request):
        name = request.match_info["name"]
        if name not in {"app.js", "style.css", "playback.mjs", "notifications.mjs", "ui.mjs"}:
            raise web.HTTPNotFound()
        return web.FileResponse(STATIC / name)

    async def session(request):
        return web.json_response({"csrf": csrf, "active_minutes": ACTIVE_MINUTES,
                                  "upload_max_mb": service.upload_max // (1024 * 1024)})

    async def health(request):
        connected = service.client.is_connected()
        return web.json_response({"ok": connected, "telegram_connected": connected},
                                 status=200 if connected else 503)

    async def notifications(request):
        raw = request.query.get("after")
        if raw is not None and (not raw.isascii() or not raw.isdecimal() or len(raw) > 19):
            raise InboxError("Некорректный cursor.", 400)
        after = int(raw) if raw is not None else None
        if after is not None and after > 9223372036854775807:
            raise InboxError("Некорректный cursor.", 400)
        return web.json_response(service.store.notifications(after))

    async def conversations(request):
        return web.json_response({"conversations": [conversation_json(r) for r in
            service.store.active()], "now": service.clock(),
            "connected": service.client.is_connected()})

    async def library(request):
        return web.json_response({"sources": service.library_json()})

    async def library_history(request):
        raw = request.query.get("before")
        if raw is not None and (not raw.isascii() or not raw.isdecimal() or len(raw) > 19):
            raise InboxError("Некорректный cursor.")
        return web.json_response(await service.library_history(
            request.match_info["source"], int(raw) if raw else None,
        ))

    async def history(request):
        async with asyncio.timeout(45):
            return web.json_response(await service.history(request.match_info["key"]))

    async def open_conversation(request):
        async with service.action_lock:
            row = service.store.open(request.match_info["key"])
            if row is None:
                raise InboxError("Разговор закрыт или время активности истекло.", 404)
            service.snapshots.pop(row.id, None)
            return web.json_response({"conversation": conversation_json(row)})

    async def close(request):
        await service.close(request.match_info["key"])
        return web.json_response({"ok": True})

    async def send(request):
        body = await request.json()
        if not isinstance(body, dict) or set(body) - {"request_id", "text", "image", "reply_to"}:
            raise InboxError("Допустимы только текст и изображение.")
        request_id = str(UUID(body.get("request_id", "")))
        raw = body.get("image")
        if raw is not None:
            if not isinstance(raw, str) or len(raw) > (MAX_IMAGE * 4 // 3 + 4):
                raise InboxError("Изображение слишком большое.", 413)
            raw = base64.b64decode(raw, validate=True)
        return web.json_response(await service.send(
            request.match_info["key"], request_id, body.get("text", ""), raw,
            reply_to=body.get("reply_to"),
        ))

    async def read_upload(request):
        reader = await request.multipart()
        values, upload = {}, None

        async def small_text(part, limit):
            value = await part.read_chunk(size=limit + 1)
            if len(value) > limit or await part.read_chunk(size=1):
                raise InboxError("Текстовое поле слишком большое.", 413)
            return value.decode("utf-8")

        try:
            async for part in reader:
                if part.name in {"request_id", "text", "reply_to"} and part.filename is None:
                    if part.name in values:
                        raise InboxError("Поле отправлено дважды.")
                    values[part.name] = await small_text(
                        part, 9000 if part.name == "text" else 100,
                    )
                elif part.name == "file" and part.filename and upload is None:
                    path = service.upload_dir / f"{secrets.token_hex(16)}.upload"
                    handle = path.open("xb")
                    os.chmod(path, 0o600)
                    size = 0
                    try:
                        while chunk := await part.read_chunk(size=256 * 1024):
                            size += len(chunk)
                            if size > service.upload_max:
                                raise InboxError("Файл превышает лимит загрузки.", 413)
                            handle.write(chunk)
                    finally:
                        handle.close()
                    if size == 0:
                        raise InboxError("Пустой файл не поддерживается.")
                    safe_name = Path(unquote(part.filename).replace("\\", "/")).name[:180]
                    safe_name = "".join(c for c in safe_name if c.isprintable()) or "attachment"
                    upload = (path, safe_name, part.headers.get(
                        "Content-Type", "application/octet-stream"))
                else:
                    raise InboxError("Некорректное поле загрузки.")
            return values, upload
        except BaseException:
            if upload:
                upload[0].unlink(missing_ok=True)
            elif "path" in locals():
                path.unlink(missing_ok=True)
            raise

    async def upload(request):
        async with service.upload_slots:
            values, item = await read_upload(request)
            try:
                request_id = str(UUID(values.get("request_id", "")))
                reply = values.get("reply_to") or None
                reply = int(reply) if reply else None
                return web.json_response(await service.send(
                    request.match_info["key"], request_id, values.get("text", ""),
                    file_path=item[0] if item else None,
                    filename=item[1] if item else None,
                    mime_type=item[2] if item else None, reply_to=reply,
                ))
            finally:
                if item:
                    item[0].unlink(missing_ok=True)

    async def send_saved(request):
        async with service.upload_slots:
            values, item = await read_upload(request)
            try:
                request_id = str(UUID(values.get("request_id", "")))
                return web.json_response(await service.send_saved(
                    request_id, values.get("text", ""), file_path=item[0] if item else None,
                    filename=item[1] if item else None, mime_type=item[2] if item else None,
                ))
            finally:
                if item:
                    item[0].unlink(missing_ok=True)

    async def media(request):
        path, mime, name, inline = await service.media(
            request.match_info["key"], int(request.match_info["message_id"]),
        )
        response = web.FileResponse(path)
        response.content_type = mime
        filename = quote(Path(name or "attachment").name[:180], safe="")
        disposition = "inline" if inline else "attachment"
        response.headers["Content-Disposition"] = f"{disposition}; filename*=UTF-8''{filename}"
        return response

    async def library_media(request):
        path, mime, name, inline = await service.library_media(
            request.match_info["source"], int(request.match_info["message_id"]),
        )
        response = web.FileResponse(path)
        response.content_type = mime
        filename = quote(Path(name or "attachment").name[:180], safe="")
        response.headers["Content-Disposition"] = (
            f"{'inline' if inline else 'attachment'}; filename*=UTF-8''{filename}"
        )
        return response

    app = web.Application(middlewares=[security], client_max_size=service.upload_max + 1024 * 1024)
    app.add_routes([
        web.get("/", index), web.get("/static/{name}", asset),
        web.get("/api/session", session), web.get("/api/health", health),
        web.get("/api/notifications", notifications),
        web.get("/api/conversations", conversations),
        web.get("/api/library", library),
        web.get(r"/api/library/{source:[a-z][a-z0-9]{0,31}}/messages", library_history),
        web.post("/api/library/saved/send", send_saved),
        web.get(r"/api/library/{source:[a-z][a-z0-9]{0,31}}/media/"
                r"{message_id:[1-9][0-9]*}", library_media),
        web.get(r"/api/conversations/{key:[a-f0-9]{32}}/messages", history),
        web.post(r"/api/conversations/{key:[a-f0-9]{32}}/open", open_conversation),
        web.post(r"/api/conversations/{key:[a-f0-9]{32}}/close", close),
        web.post(r"/api/conversations/{key:[a-f0-9]{32}}/send", send),
        web.post(r"/api/conversations/{key:[a-f0-9]{32}}/upload", upload),
        web.get(r"/api/conversations/{key:[a-f0-9]{32}}/media/{message_id:[1-9][0-9]*}", media),
    ])
    return app


@contextlib.asynccontextmanager
async def serve_inbox(service, *, port=PORT):
    runner = web.AppRunner(create_app(service, port=port), access_log=None, shutdown_timeout=100)
    await runner.setup()
    site = web.TCPSite(runner, HOST, port)
    cleanup_task = None

    async def maintenance():
        while True:
            await asyncio.sleep(30)
            try:
                async with service.media_lock:
                    await service.cleanup()
            except Exception as exc:
                logger.warning("Inbox cleanup failed (%s)", type(exc).__name__)

    try:
        await service.cleanup()
        await site.start()
        logger.info("Mention inbox listening on http://127.0.0.1:%s", port)
        cleanup_task = asyncio.create_task(maintenance())
        yield
    finally:
        if cleanup_task:
            cleanup_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await cleanup_task
        await runner.cleanup()
