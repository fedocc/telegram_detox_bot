from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import logging
import secrets
from pathlib import Path
from urllib.parse import quote
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
                if request.content_type != "application/json":
                    raise InboxError("Требуется application/json.", 415)
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
                "img-src 'self' blob:; media-src 'self' blob:; connect-src 'self'; "
                "base-uri 'none'; frame-ancestors 'none'; form-action 'none'",
        })
        return response

    async def index(request):
        return web.FileResponse(STATIC / "index.html")

    async def asset(request):
        name = request.match_info["name"]
        if name not in {"app.js", "style.css", "playback.mjs"}:
            raise web.HTTPNotFound()
        return web.FileResponse(STATIC / name)

    async def session(request):
        return web.json_response({"csrf": csrf, "active_minutes": ACTIVE_MINUTES})

    async def health(request):
        connected = service.client.is_connected()
        return web.json_response({"ok": connected, "telegram_connected": connected},
                                 status=200 if connected else 503)

    async def conversations(request):
        return web.json_response({"conversations": [conversation_json(r) for r in
            service.store.active()], "now": service.clock(),
            "connected": service.client.is_connected()})

    async def history(request):
        async with asyncio.timeout(45):
            return web.json_response(await service.history(request.match_info["key"]))

    async def close(request):
        await service.close(request.match_info["key"])
        return web.json_response({"ok": True})

    async def send(request):
        body = await request.json()
        if not isinstance(body, dict) or set(body) - {"request_id", "text", "image"}:
            raise InboxError("Допустимы только текст и изображение.")
        request_id = str(UUID(body.get("request_id", "")))
        raw = body.get("image")
        if raw is not None:
            if not isinstance(raw, str) or len(raw) > (MAX_IMAGE * 4 // 3 + 4):
                raise InboxError("Изображение слишком большое.", 413)
            raw = base64.b64decode(raw, validate=True)
        return web.json_response(await service.send(
            request.match_info["key"], request_id, body.get("text", ""), raw,
        ))

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

    app = web.Application(middlewares=[security], client_max_size=14 * 1024 * 1024)
    app.add_routes([
        web.get("/", index), web.get("/static/{name}", asset),
        web.get("/api/session", session), web.get("/api/health", health),
        web.get("/api/conversations", conversations),
        web.get(r"/api/conversations/{key:[a-f0-9]{32}}/messages", history),
        web.post(r"/api/conversations/{key:[a-f0-9]{32}}/close", close),
        web.post(r"/api/conversations/{key:[a-f0-9]{32}}/send", send),
        web.get(r"/api/conversations/{key:[a-f0-9]{32}}/media/{message_id:[1-9][0-9]*}", media),
    ])
    return app


@contextlib.asynccontextmanager
async def serve_inbox(service):
    runner = web.AppRunner(create_app(service), access_log=None, shutdown_timeout=100)
    await runner.setup()
    site = web.TCPSite(runner, HOST, PORT)
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
        logger.info("Mention inbox listening on http://127.0.0.1:8787")
        cleanup_task = asyncio.create_task(maintenance())
        yield
    finally:
        if cleanup_task:
            cleanup_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await cleanup_task
        await runner.cleanup()
