from __future__ import annotations

import asyncio
import json

from aiohttp import web

from app.inbox.device_auth import (
    DEVICE_COOKIE,
    ENROLL_COOKIE,
    SESSION_COOKIE,
    SESSION_SECONDS,
    TRUST_SECONDS,
)
from app.inbox.errors import InboxError


async def small_json(request, required):
    if request.content_type != "application/json":
        raise InboxError("Нужен JSON-запрос.", 415)
    chunks, size = [], 0
    async with asyncio.timeout(10):
        async for chunk in request.content.iter_chunked(8192):
            size += len(chunk)
            if size > 65536:
                raise InboxError("Слишком большой запрос.", 413)
            chunks.append(chunk)
    try:
        value = json.loads(b"".join(chunks))
    except (ValueError, UnicodeError):
        raise InboxError("Некорректный запрос.") from None
    if not isinstance(value, dict) or set(value) != set(required):
        raise InboxError("Некорректный запрос.")
    return value


def cookie(response, name, value, age):
    response.set_cookie(
        name, value, max_age=age, secure=True, httponly=True, samesite="Strict", path="/"
    )


def clear_cookie(response, name):
    response.del_cookie(name, path="/", secure=True, httponly=True, samesite="Strict")


def add_device_routes(app, manager, *, public, static, session_key):
    def private_only():
        if public:
            raise InboxError("Раздел доступен только с доверенного Mac.", 403)

    def public_only(request):
        if not public or request.headers.get("Origin") != manager.origin:
            raise InboxError("Недопустимый Origin.", 403)

    async def login_page(request):
        if not public:
            raise InboxError("Вход по ключу доступен по внешнему адресу.", 404)
        return web.FileResponse(static / "login.html")

    async def devices_page(request):
        private_only()
        return web.FileResponse(static / "devices.html")

    async def state(request):
        if not public:
            raise InboxError("Запрос не поддерживается.", 404)
        result = manager.state(request.cookies.get(DEVICE_COOKIE))
        result["authenticated"] = request.get(session_key) is not None
        return web.json_response(result)

    async def register_options(request):
        public_only(request)
        body = await small_json(request, {"invitation", "label"})
        if manager.state(request.cookies.get(DEVICE_COOKIE))["state"] in {"approved", "pending"}:
            raise InboxError("Этот браузер уже подключён или ожидает подтверждения.", 409)
        result, binding = manager.registration_options(
            body["invitation"], body["label"], ip=manager.ip(request)
        )
        response = web.json_response(result)
        cookie(response, ENROLL_COOKIE, binding, 300)
        return response

    async def register_finish(request):
        public_only(request)
        body = await small_json(request, {"ceremony", "credential"})
        binding = request.cookies.get(ENROLL_COOKIE)
        result = await manager.finish_registration(
            body["ceremony"], body["credential"], binding, ip=manager.ip(request)
        )
        response = web.json_response(result)
        cookie(response, DEVICE_COOKIE, binding, TRUST_SECONDS)
        clear_cookie(response, ENROLL_COOKIE)
        return response

    async def login_options(request):
        public_only(request)
        await small_json(request, set())
        return web.json_response(
            manager.login_options(request.cookies.get(DEVICE_COOKIE), ip=manager.ip(request))
        )

    async def login_finish(request):
        public_only(request)
        body = await small_json(request, {"ceremony", "credential"})
        token = await manager.finish_login(
            body["ceremony"],
            body["credential"],
            request.cookies.get(DEVICE_COOKIE),
            ip=manager.ip(request),
        )
        response = web.json_response({"ok": True})
        cookie(response, SESSION_COOKIE, token, SESSION_SECONDS)
        return response

    async def logout(request):
        public_only(request)
        await small_json(request, set())
        manager.logout(
            request.cookies.get(DEVICE_COOKIE),
            request.cookies.get(SESSION_COOKIE),
            ip=manager.ip(request),
        )
        response = web.json_response({"ok": True})
        clear_cookie(response, SESSION_COOKIE)
        return response

    async def overview(request):
        private_only()
        return web.json_response(manager.overview())

    async def invite(request):
        private_only()
        await small_json(request, set())
        return web.json_response(manager.create_invite())

    async def cancel_invite(request):
        private_only()
        await small_json(request, set())
        manager.cancel_invite(request.match_info["identifier"])
        return web.json_response({"ok": True})

    async def approve(request):
        private_only()
        body = await small_json(request, {"code"})
        manager.approve(request.match_info["identifier"], body["code"])
        return web.json_response({"ok": True})

    async def revoke(request):
        private_only()
        await small_json(request, set())
        manager.revoke(request.match_info["identifier"])
        return web.json_response({"ok": True})

    app.add_routes(
        [
            web.get("/login", login_page),
            web.get("/auth/status", state),
            web.post("/auth/register/options", register_options),
            web.post("/auth/register/finish", register_finish),
            web.post("/auth/login/options", login_options),
            web.post("/auth/login/finish", login_finish),
            web.post("/auth/logout", logout),
            web.get("/devices", devices_page),
            web.get("/api/devices", overview),
            web.post("/api/devices/invitations", invite),
            web.post(r"/api/devices/invitations/{identifier:[a-f0-9]{32}}/cancel", cancel_invite),
            web.post(r"/api/devices/{identifier:[a-f0-9]{32}}/approve", approve),
            web.post(r"/api/devices/{identifier:[a-f0-9]{32}}/revoke", revoke),
        ]
    )
