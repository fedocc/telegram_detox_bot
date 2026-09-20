from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import logging
import os
import secrets
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit
from uuid import UUID
from zoneinfo import ZoneInfo

from aiohttp import web

from app.inbox.service import MAX_IMAGE, InboxError, conversation_json
from app.inbox.store import ACTIVE_MINUTES

HOST = "127.0.0.1"
PORT = 8787
STATIC = Path(__file__).parent / "static"
logger = logging.getLogger(__name__)


def create_app(service, *, port=PORT, allowed_origins=None, library_management_enabled=False):
    csrf = secrets.token_urlsafe(32)
    local_origin = f"http://127.0.0.1:{port}"
    allowed_origins = frozenset(allowed_origins or (local_origin,)) | {local_origin}
    allowed_hosts = frozenset(urlsplit(origin).netloc for origin in allowed_origins)

    @web.middleware
    async def security(request, handler):
        try:
            if request.host not in allowed_hosts:
                raise InboxError("Недопустимый Host.", 403)
            origin = request.headers.get("Origin")
            if origin and origin not in allowed_origins:
                raise InboxError("Недопустимый Origin.", 403)
            if request.headers.get("Sec-Fetch-Site") not in {None, "same-origin", "none"}:
                raise InboxError("Межсайтовый запрос запрещён.", 403)
            if request.method not in {"GET", "HEAD"}:
                if (origin not in allowed_origins or not secrets.compare_digest(
                        request.headers.get("X-Inbox-CSRF", ""), csrf)):
                    raise InboxError("Перезагрузите страницу перед отправкой.", 403)
                if request.content_type not in {"application/json", "multipart/form-data"}:
                    raise InboxError("Неподдерживаемый формат запроса.", 415)
            response = await handler(request)
        except InboxError as exc:
            response = web.json_response({"error": str(exc)}, status=exc.status)
            if exc.retry_after is not None:
                response.headers["Retry-After"] = str(exc.retry_after)
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
            "Cache-Control": ("no-store" if request.path.startswith("/api/")
                              else "no-cache" if request.path in {"/", "/sw.js"}
                              else "public, max-age=3600"),
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer", "X-Frame-Options": "DENY",
            "Cross-Origin-Resource-Policy": "same-origin",
            "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; "
                "img-src 'self' blob:; media-src 'self' blob:; manifest-src 'self'; "
                "connect-src 'self' http://127.0.0.1:8788; worker-src 'self'; "
                "base-uri 'none'; frame-ancestors 'none'; form-action 'none'",
        })
        return response

    async def index(request):
        return web.FileResponse(STATIC / "index.html")

    async def asset(request):
        name = request.match_info["name"]
        if name not in {"app.js", "style.css", "playback.mjs", "notifications.mjs",
                        "push.mjs", "ui.mjs",
                        "app-icon.svg", "app-icon-180.png", "app-icon-512.png"}:
            raise web.HTTPNotFound()
        return web.FileResponse(STATIC / name)

    async def manifest(request):
        return web.FileResponse(STATIC / "manifest.webmanifest")

    async def service_worker(request):
        return web.FileResponse(STATIC / "sw.js")

    async def session(request):
        return web.json_response({"csrf": csrf, "active_minutes": ACTIVE_MINUTES,
                                  "upload_max_mb": service.upload_max // (1024 * 1024),
                                  "library_management_enabled": library_management_enabled})

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

    async def push_config(request):
        return web.json_response({
            "configured": service.push.configured,
            "public_key": service.push.public_key if service.push.configured else "",
        })

    async def push_subscribe(request):
        body = await request.json()
        if not isinstance(body, dict) or set(body) - {"subscription", "device_label"}:
            raise InboxError("Некорректная push-подписка.")
        try:
            return web.json_response(service.push.subscribe(
                body.get("subscription"), body.get("device_label", "")
            ))
        except ValueError:
            raise InboxError("Некорректная push-подписка.") from None
        except RuntimeError:
            raise InboxError("Push-уведомления ещё не настроены.", 503) from None

    async def push_unsubscribe(request):
        body = await request.json()
        if not isinstance(body, dict) or set(body) != {"subscription"}:
            raise InboxError("Некорректная push-подписка.")
        try:
            return web.json_response(service.push.unsubscribe(body["subscription"]))
        except ValueError:
            raise InboxError("Некорректная push-подписка.") from None

    def positive_query(request, name):
        raw = request.query.get(name)
        if raw is None:
            return None
        if (not raw.isascii() or not raw.isdecimal() or len(raw) > 19
                or not 0 < int(raw) <= 9223372036854775807):
            raise InboxError("Некорректный cursor.", 400)
        return int(raw)

    async def conversations(request):
        rows = service.store.active()
        return web.json_response({"conversations": [conversation_json(row) for row in rows],
            "now": service.clock(), "connected": service.client.is_connected(),
            "badge": service.store.unread_total(rows)})

    async def badge(request):
        return web.json_response({"badge": service.store.unread_total()})

    async def library(request):
        return web.json_response({"sources": service.library_json()})

    async def morning_digest(request):
        from app.inbox.digest import latest_digest

        return web.json_response({"digest": latest_digest(service.store.factory)})

    async def morning_digest_history(request):
        from app.inbox.digest import digest_history

        return web.json_response({"digests": digest_history(
            service.store.factory, service.timezone,
            now=datetime.fromtimestamp(service.clock(), ZoneInfo(service.timezone)),
        )})

    async def library_dialogs(request):
        if not library_management_enabled:
            raise InboxError("Управление библиотекой временно отключено.", 404)
        async with asyncio.timeout(45):
            return web.json_response(await service.library_dialogs(request.query.get("q", "")))

    async def library_preferences(request):
        if not library_management_enabled:
            raise InboxError("Управление библиотекой временно отключено.", 404)
        body = await request.json()
        fields = {
            "token", "source_id", "library_enabled", "notifications_muted",
            "allow_bot_write", "digest_excluded",
        }
        if not isinstance(body, dict) or set(body) - fields:
            raise InboxError("Некорректные настройки библиотеки.")
        identifiers = [body.get(name) for name in ("source_id", "token")
                       if body.get(name) is not None]
        if (len(identifiers) != 1 or not isinstance(identifiers[0], str)
                or not identifiers[0]):
            raise InboxError("Сначала выберите Telegram-чат.")
        identifier = identifiers[0]
        preferences = {key: value for key, value in body.items()
                       if key not in {"token", "source_id"}}
        return web.json_response(await service.update_library(identifier, preferences))

    async def library_reorder(request):
        if not library_management_enabled:
            raise InboxError("Управление библиотекой временно отключено.", 404)
        body = await request.json()
        if not isinstance(body, dict) or set(body) != {"source_ids"}:
            raise InboxError("Некорректный порядок библиотеки.")
        return web.json_response(service.reorder_library(body["source_ids"]))

    async def open_library(request):
        async with service.action_lock:
            return web.json_response(service.open_library(request.match_info["source"]))

    async def library_history(request):
        return web.json_response(await service.library_history(
            request.match_info["source"], positive_query(request, "before"),
        ))

    async def library_pins(request):
        async with asyncio.timeout(45):
            return web.json_response(await service.library_pins(
                request.match_info["source"]
            ))

    async def library_message(request):
        async with asyncio.timeout(45):
            return web.json_response(await service.library_message(
                request.match_info["source"], int(request.match_info["message_id"]),
            ))

    async def library_search(request):
        async with asyncio.timeout(45):
            return web.json_response(await service.library_search(
                request.match_info["source"], request.query.get("q", ""),
                positive_query(request, "before"),
            ))

    async def history(request):
        async with asyncio.timeout(45):
            return web.json_response(await service.history(request.match_info["key"]))

    async def conversation_pins(request):
        async with asyncio.timeout(45):
            return web.json_response(await service.conversation_pins(
                request.match_info["key"]
            ))

    async def conversation_message(request):
        async with asyncio.timeout(45):
            return web.json_response(await service.conversation_message(
                request.match_info["key"], int(request.match_info["message_id"]),
            ))

    async def conversation_search(request):
        async with asyncio.timeout(45):
            return web.json_response(await service.conversation_search(
                request.match_info["key"], request.query.get("q", ""),
                positive_query(request, "before"),
            ))

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
        if request.content_type != "multipart/form-data":
            raise InboxError("Для загрузки требуется multipart/form-data.", 415)
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
        service.require(request.match_info["key"])
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

    async def send_library(request):
        # Resolve the opaque allowlisted source before parsing a potentially large
        # multipart body. Unknown/disabled sources fail closed with 404.
        service.library_source(request.match_info["source"])
        async with service.upload_slots:
            values, item = await read_upload(request)
            try:
                request_id = str(UUID(values.get("request_id", "")))
                reply = values.get("reply_to") or None
                reply = int(reply) if reply else None
                return web.json_response(await service.send_library(
                    request.match_info["source"], request_id, values.get("text", ""),
                    file_path=item[0] if item else None,
                    filename=item[1] if item else None,
                    mime_type=item[2] if item else None,
                    reply_to=reply,
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

    async def custom_emoji_media(request):
        path, mime, name, inline = await service.custom_emoji_media(
            int(request.match_info["document_id"])
        )
        response = web.FileResponse(path)
        response.content_type = mime
        response.headers["Content-Disposition"] = (
            f"{'inline' if inline else 'attachment'}; filename*=UTF-8''{quote(name, safe='')}"
        )
        response.headers["Cache-Control"] = "private, max-age=31536000, immutable"
        return response

    app = web.Application(middlewares=[security], client_max_size=service.upload_max + 1024 * 1024)
    app.add_routes([
        web.get("/", index), web.get("/static/{name}", asset),
        web.get("/manifest.webmanifest", manifest), web.get("/sw.js", service_worker),
        web.get("/api/session", session), web.get("/api/health", health),
        web.get("/api/notifications", notifications),
        web.get("/api/push/config", push_config),
        web.post("/api/push/subscriptions", push_subscribe),
        web.post("/api/push/unsubscribe", push_unsubscribe),
        web.get("/api/conversations", conversations),
        web.get("/api/badge", badge),
        web.get("/api/library", library),
        web.get("/api/digest/latest", morning_digest),
        web.get("/api/digest/history", morning_digest_history),
        web.get("/api/library/dialogs", library_dialogs),
        web.post("/api/library/preferences", library_preferences),
        web.post("/api/library/reorder", library_reorder),
        web.post(r"/api/library/{source:[A-Za-z0-9_-]{1,64}}/open", open_library),
        web.get(r"/api/library/{source:[A-Za-z0-9_-]{1,64}}/messages", library_history),
        web.get(r"/api/library/{source:[A-Za-z0-9_-]{1,64}}/pins", library_pins),
        web.get(r"/api/library/{source:[A-Za-z0-9_-]{1,64}}/messages/"
                r"{message_id:[1-9][0-9]*}", library_message),
        web.get(r"/api/library/{source:[A-Za-z0-9_-]{1,64}}/search", library_search),
        web.post("/api/library/saved/send", send_saved),
        web.post(r"/api/library/{source:[A-Za-z0-9_-]{1,64}}/send", send_library),
        web.get(r"/api/library/{source:[A-Za-z0-9_-]{1,64}}/media/"
                r"{message_id:[1-9][0-9]*}", library_media),
        web.get(r"/api/custom-emoji/{document_id:[1-9][0-9]*}", custom_emoji_media),
        web.get(r"/api/conversations/{key:[a-f0-9]{32}}/messages", history),
        web.get(r"/api/conversations/{key:[a-f0-9]{32}}/pins", conversation_pins),
        web.get(r"/api/conversations/{key:[a-f0-9]{32}}/messages/"
                r"{message_id:[1-9][0-9]*}", conversation_message),
        web.get(r"/api/conversations/{key:[a-f0-9]{32}}/search", conversation_search),
        web.post(r"/api/conversations/{key:[a-f0-9]{32}}/open", open_conversation),
        web.post(r"/api/conversations/{key:[a-f0-9]{32}}/close", close),
        web.post(r"/api/conversations/{key:[a-f0-9]{32}}/send", send),
        web.post(r"/api/conversations/{key:[a-f0-9]{32}}/upload", upload),
        web.get(r"/api/conversations/{key:[a-f0-9]{32}}/media/{message_id:[1-9][0-9]*}", media),
    ])
    return app


@contextlib.asynccontextmanager
async def serve_inbox(service, *, port=PORT, allowed_origins=None,
                      library_management_enabled=False):
    runner = web.AppRunner(create_app(
        service, port=port, allowed_origins=allowed_origins,
        library_management_enabled=library_management_enabled,
    ),
                           access_log=None, shutdown_timeout=100)
    await runner.setup()
    site = web.TCPSite(runner, HOST, port)
    cleanup_task = None
    push_task = None

    async def maintenance():
        while True:
            await asyncio.sleep(30)
            try:
                async with service.media_lock:
                    await service.cleanup()
            except Exception as exc:
                logger.warning("Inbox cleanup failed (%s)", type(exc).__name__)

    async def push_maintenance():
        while True:
            await asyncio.sleep(4)
            try:
                await service.push.deliver_pending()
            except Exception as exc:
                # Subscription material and delivery payloads are intentionally omitted.
                logger.warning("Web Push delivery failed (%s)", type(exc).__name__)

    try:
        await service.cleanup()
        await site.start()
        logger.info("Mention inbox listening on http://127.0.0.1:%s", port)
        cleanup_task = asyncio.create_task(maintenance())
        push_task = asyncio.create_task(push_maintenance())
        yield
    finally:
        for task in (cleanup_task, push_task):
            if task:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        await runner.cleanup()
