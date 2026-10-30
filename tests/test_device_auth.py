import asyncio
import hashlib
import json
import secrets
from types import SimpleNamespace

import cbor2
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from sqlalchemy import select
from webauthn.helpers import bytes_to_base64url

from app.db.session import init_db
from app.db.tables import InboxAuthAudit, InboxTrustedDevice
from app.inbox.device_auth import (
    DEVICE_COOKIE,
    ENROLL_COOKIE,
    SESSION_COOKIE,
    SESSION_SECONDS,
    DeviceAuth,
    digest,
)
from app.inbox.errors import InboxError
from app.inbox.web import create_app
from tests.test_inbox import service as service
from tests.test_web_push import push_fixture as push_fixture

ORIGIN = "https://detox.example.test"
HOST = "detox.example.test"


@pytest.fixture()
def auth(settings):
    clock = [1_800_000_000.0]
    manager = DeviceAuth(init_db(settings), ORIGIN, clock=lambda: clock[0])
    manager.test_clock = clock
    return manager


def registration(options, *, origin=ORIGIN, uv=True):
    private = ec.generate_private_key(ec.SECP256R1())
    numbers = private.public_key().public_numbers()
    public = {1: 2, 3: -7, -1: 1, -2: numbers.x.to_bytes(32), -3: numbers.y.to_bytes(32)}
    identifier = secrets.token_bytes(32)
    authenticator = (
        hashlib.sha256(HOST.encode()).digest()
        + bytes([0x41 | (4 if uv else 0)])
        + bytes(4)
        + bytes(16)
        + len(identifier).to_bytes(2)
        + identifier
        + cbor2.dumps(public)
    )
    client = json.dumps(
        {
            "type": "webauthn.create",
            "challenge": options["challenge"],
            "origin": origin,
            "crossOrigin": False,
        }
    ).encode()
    credential = {
        "id": bytes_to_base64url(identifier),
        "rawId": bytes_to_base64url(identifier),
        "type": "public-key",
        "response": {
            "clientDataJSON": bytes_to_base64url(client),
            "attestationObject": bytes_to_base64url(
                cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": authenticator})
            ),
            "transports": ["internal"],
        },
    }
    return credential, SimpleNamespace(
        private=private, identifier=identifier, user_handle=options["user"]["id"]
    )


def assertion(options, key, *, count=1, origin=ORIGIN, uv=True):
    client = json.dumps(
        {
            "type": "webauthn.get",
            "challenge": options["challenge"],
            "origin": origin,
            "crossOrigin": False,
        }
    ).encode()
    authenticator = (
        hashlib.sha256(HOST.encode()).digest() + bytes([1 | (4 if uv else 0)]) + count.to_bytes(4)
    )
    signature = key.private.sign(
        authenticator + hashlib.sha256(client).digest(), ec.ECDSA(hashes.SHA256())
    )
    return {
        "id": bytes_to_base64url(key.identifier),
        "rawId": bytes_to_base64url(key.identifier),
        "type": "public-key",
        "response": {
            "clientDataJSON": bytes_to_base64url(client),
            "authenticatorData": bytes_to_base64url(authenticator),
            "signature": bytes_to_base64url(signature),
            "userHandle": key.user_handle,
        },
    }


async def enroll(auth, *, approve=True, manual=False):
    invite = auth.create_invite()
    token = invite["code"] if manual else invite["url"].split("pair=")[1]
    ceremony, binding = auth.registration_options(token, "Мой iPhone")
    credential, key = registration(ceremony["options"])
    state = await auth.finish_registration(ceremony["ceremony"], credential, binding)
    assert state["state"] == "pending"
    with auth.factory() as db:
        device = db.scalar(
            select(InboxTrustedDevice).where(InboxTrustedDevice.binding_hash == digest(binding))
        )
    if approve:
        auth.approve(device.id, state["code"])
    return binding, key, device


async def login(auth, binding, key, **kwargs):
    ceremony = auth.login_options(binding)
    credential = assertion(ceremony["options"], key, **kwargs)
    token = await auth.finish_login(ceremony["ceremony"], credential, binding)
    return token, ceremony, credential


async def test_real_crypto_enrollment_approval_login_and_replay(auth):
    binding, key, device = await enroll(auth)
    token, ceremony, credential = await login(auth, binding, key)
    session = auth.authenticate(binding, token)
    assert session["device_id"] == device.id
    assert session["csrf"] and session["expires_at"] == auth.clock() + SESSION_SECONDS
    with pytest.raises(InboxError):
        await auth.finish_login(ceremony["ceremony"], credential, binding)
    assert auth.authenticate(secrets.token_urlsafe(32), token) is None
    assert auth.authenticate(binding, secrets.token_urlsafe(32)) is None


async def test_pending_device_cannot_login_and_code_must_match(auth):
    binding, key, device = await enroll(auth, approve=False, manual=True)
    with pytest.raises(InboxError):
        auth.login_options(binding)
    with pytest.raises(InboxError):
        auth.approve(device.id, "wrong")
    auth.approve(device.id, device.confirmation_code)
    token, _, _ = await login(auth, binding, key)
    assert auth.authenticate(binding, token)


@pytest.mark.parametrize("origin,uv", [("https://evil.example", True), (ORIGIN, False)])
async def test_registration_rejects_wrong_origin_or_missing_user_verification(auth, origin, uv):
    invitation = auth.create_invite()
    ceremony, binding = auth.registration_options(invitation["code"], "iPhone")
    credential, _ = registration(ceremony["options"], origin=origin, uv=uv)
    with pytest.raises(InboxError):
        await auth.finish_registration(ceremony["ceremony"], credential, binding)
    assert auth.overview()["devices"] == []


@pytest.mark.parametrize("kind", ["origin", "uv", "signature", "handle", "challenge"])
async def test_authentication_rejects_tampered_assertions(auth, kind):
    binding, key, _ = await enroll(auth)
    ceremony = auth.login_options(binding)
    options = dict(ceremony["options"])
    if kind == "challenge":
        options["challenge"] = bytes_to_base64url(secrets.token_bytes(32))
    credential = assertion(
        options, key, origin="https://evil.example" if kind == "origin" else ORIGIN, uv=kind != "uv"
    )
    if kind == "signature":
        credential["response"]["signature"] = bytes_to_base64url(bytes(72))
    if kind == "handle":
        credential["response"]["userHandle"] = bytes_to_base64url(bytes(32))
    with pytest.raises(InboxError):
        await auth.finish_login(ceremony["ceremony"], credential, binding)


async def test_expiry_revocation_and_session_rotation(auth):
    binding, key, device = await enroll(auth)
    first, _, _ = await login(auth, binding, key)
    second, _, _ = await login(auth, binding, key, count=2)
    assert auth.authenticate(binding, first) is None
    assert auth.authenticate(binding, second)
    auth.revoke(device.id)
    assert auth.authenticate(binding, second) is None
    with pytest.raises(InboxError):
        auth.login_options(binding)


async def test_fixed_session_expiry_is_not_extended_by_requests(auth):
    binding, key, _ = await enroll(auth)
    token, _, _ = await login(auth, binding, key)
    auth.test_clock[0] += SESSION_SECONDS - 1
    assert auth.authenticate(binding, token)
    auth.test_clock[0] += 2
    assert auth.authenticate(binding, token) is None


async def test_invitation_is_one_time_and_cannot_finish_after_cancellation(auth):
    invitation = auth.create_invite()
    ceremony, binding = auth.registration_options(invitation["code"], "iPhone")
    credential, _ = registration(ceremony["options"])
    auth.cancel_invite(invitation["id"])
    with pytest.raises(InboxError):
        await auth.finish_registration(ceremony["ceremony"], credential, binding)


async def test_passkey_alone_does_not_authorize_another_browser(auth):
    binding, key, _ = await enroll(auth)
    ceremony = auth.login_options(binding)
    credential = assertion(ceremony["options"], key)
    with pytest.raises(InboxError):
        await auth.finish_login(ceremony["ceremony"], credential, secrets.token_urlsafe(32))


async def test_public_listener_protects_every_data_route_and_administration(auth, service):
    app = create_app(service, device_auth=auth, public_auth=True)
    async with TestClient(TestServer(app), headers={"Host": HOST, "Origin": ORIGIN}) as client:
        response = await client.get("/")
        assert response.status == 200 and "Только ваши устройства" in await response.text()
        paths = [
            "/api/session",
            "/api/conversations",
            "/api/library",
            "/api/contacts",
            "/api/notifications",
            "/api/health",
            "/api/push/config",
            "/api/conversations/" + "a" * 32 + "/media/1",
        ]
        for path in paths:
            response = await client.get(path)
            assert response.status == 401, path
            assert response.headers["Cache-Control"] == "no-store"
            assert (await response.json())["auth_required"]
        assert (await client.head(paths[-1])).status == 401
        assert (await client.post("/api/manual-open", json={})).status == 401
        assert (await client.get("/api/devices")).status == 403
        assert (await client.post("/api/devices/invitations", json={})).status == 403
        assert (await client.get("/devices")).status == 403
        assert (await client.get("/api/session", headers={"Host": "127.0.0.1:8789"})).status == 403
        assert (
            await client.get("/auth/status", headers={"Origin": "https://evil.example"})
        ).status == 403


async def test_authenticated_public_session_has_own_csrf_and_cannot_administer(auth, service):
    binding, key, device = await enroll(auth)
    token, _, _ = await login(auth, binding, key)
    cookies = f"{DEVICE_COOKIE}={binding}; {SESSION_COOKIE}={token}"
    app = create_app(service, device_auth=auth, public_auth=True)
    async with TestClient(
        TestServer(app), headers={"Host": HOST, "Origin": ORIGIN, "Cookie": cookies}
    ) as client:
        response = await client.get("/api/session")
        data = await response.json()
        assert response.status == 200 and data["public_auth"] and not data["device_management"]
        assert (await client.get("/api/conversations")).status == 200
        assert (await client.get("/api/devices")).status == 403
        assert (await client.post("/api/manual-open", json={})).status == 403
        auth.revoke(device.id)
        assert (await client.get("/api/conversations")).status == 401


def test_rate_limit_is_bounded_and_audit_has_no_credentials(auth):
    for _ in range(12):
        auth.rate_limit("203.0.113.5", "login")
    with pytest.raises(InboxError) as error:
        auth.rate_limit("203.0.113.5", "login")
    assert error.value.status == 429
    auth.audit_failure("auth_rejected", "203.0.113.5")
    with auth.factory() as db:
        record = db.scalar(select(InboxAuthAudit))
        assert record.event == "auth_rejected"
    assert "credential" not in json.dumps(auth.overview())


async def test_complete_http_flow_cookie_flags_private_approval_and_logout(auth, service):
    invite = auth.create_invite()
    public_app = create_app(service, device_auth=auth, public_auth=True)
    private_app = create_app(service, device_auth=auth)
    async with (
        TestClient(TestServer(public_app), headers={"Host": HOST, "Origin": ORIGIN}) as phone,
        TestClient(TestServer(private_app), headers={"Host": "127.0.0.1:8787"}) as mac,
    ):
        response = await phone.post(
            "/auth/register/options", json={"invitation": invite["code"], "label": "Мой iPhone"}
        )
        assert response.status == 200
        ceremony = await response.json()
        enroll_cookie = response.cookies[ENROLL_COOKIE]
        for attribute in ["secure", "httponly"]:
            assert enroll_cookie[attribute]
        assert enroll_cookie["samesite"] == "Strict" and not enroll_cookie["domain"]
        credential, key = registration(ceremony["options"])
        response = await phone.post(
            "/auth/register/finish",
            json={"ceremony": ceremony["ceremony"], "credential": credential},
            headers={"Cookie": f"{ENROLL_COOKIE}={enroll_cookie.value}"},
        )
        assert response.status == 200
        state = await response.json()
        binding = response.cookies[DEVICE_COOKIE].value
        cookie_header = {"Cookie": f"{DEVICE_COOKIE}={binding}"}
        assert (await phone.get("/api/session", headers=cookie_header)).status == 401
        assert (
            await phone.post("/auth/login/options", json={}, headers=cookie_header)
        ).status == 401
        local_session = await (await mac.get("/api/session")).json()
        local_headers = {"Origin": "http://127.0.0.1:8787", "X-Inbox-CSRF": local_session["csrf"]}
        devices = await (await mac.get("/api/devices")).json()
        identifier = devices["devices"][0]["id"]
        assert (
            await mac.post(f"/api/devices/{identifier}/approve", json={"code": state["code"]})
        ).status == 403
        response = await mac.post(
            f"/api/devices/{identifier}/approve",
            json={"code": state["code"]},
            headers=local_headers,
        )
        assert response.status == 200
        ceremony = await (
            await phone.post("/auth/login/options", json={}, headers=cookie_header)
        ).json()
        response = await phone.post(
            "/auth/login/finish",
            json={
                "ceremony": ceremony["ceremony"],
                "credential": assertion(ceremony["options"], key),
            },
            headers=cookie_header,
        )
        assert response.status == 200
        session_cookie = response.cookies[SESSION_COOKIE]
        assert session_cookie["secure"] and session_cookie["httponly"]
        assert session_cookie["path"] == "/" and session_cookie["samesite"] == "Strict"
        cookies = {"Cookie": f"{DEVICE_COOKIE}={binding}; {SESSION_COOKIE}={session_cookie.value}"}
        public_session = await (await phone.get("/api/session", headers=cookies)).json()
        assert public_session["csrf"] != local_session["csrf"]
        wrong_csrf = {**cookies, "X-Inbox-CSRF": local_session["csrf"]}
        assert (await phone.post("/api/manual-open", json={}, headers=wrong_csrf)).status == 403
        correct_csrf = {**cookies, "X-Inbox-CSRF": public_session["csrf"]}
        assert (await phone.post("/api/manual-open", json={}, headers=correct_csrf)).status == 400
        assert service.manual_open_status()["used"] == 0 and not service.store.active()
        assert (await phone.post("/auth/logout", json={}, headers=cookies)).status == 200
        assert (await phone.get("/api/session", headers=cookies)).status == 401


async def test_revocation_cancels_active_authenticated_requests(auth, service):
    binding, key, device = await enroll(auth)
    token, _, _ = await login(auth, binding, key)
    closed = asyncio.Event()

    async def stream(request):
        response = web.StreamResponse()
        await response.prepare(request)
        try:
            await asyncio.Event().wait()
        finally:
            closed.set()

    app = create_app(service, device_auth=auth, public_auth=True)
    app.router.add_get("/api/stream-probe", stream)
    async with TestClient(
        TestServer(app),
        headers={
            "Host": HOST,
            "Origin": ORIGIN,
            "Cookie": f"{DEVICE_COOKIE}={binding}; {SESSION_COOKIE}={token}",
        },
    ) as client:
        response = await client.get("/api/stream-probe")
        assert device.id in auth.active
        auth.revoke(device.id)
        await asyncio.wait_for(closed.wait(), timeout=1)
        response.close()
        assert (await client.get("/api/session")).status == 401


async def test_expired_invitation_and_pending_approval_cannot_be_reused(auth):
    invitation = auth.create_invite()
    auth.test_clock[0] += 301
    with pytest.raises(InboxError):
        auth.registration_options(invitation["code"], "iPhone")
    binding, _, device = await enroll(auth, approve=False)
    auth.test_clock[0] += 901
    assert auth.state(binding)["state"] == "expired"
    with pytest.raises(InboxError):
        auth.approve(device.id, device.confirmation_code)


async def test_public_auth_request_size_and_cross_site_guards(auth, service):
    app = create_app(service, device_auth=auth, public_auth=True)
    async with TestClient(TestServer(app), headers={"Host": HOST, "Origin": ORIGIN}) as client:
        response = await client.post(
            "/auth/login/finish", json={"ceremony": "x" * 70000, "credential": {}}
        )
        assert response.status == 413
        assert (
            await client.post(
                "/auth/login/options", json={}, headers={"Sec-Fetch-Site": "cross-site"}
            )
        ).status == 403
        assert (
            await client.get(
                "/api/devices",
                headers={"X-Forwarded-Host": "127.0.0.1:8787", "X-Forwarded-For": "127.0.0.1"},
            )
        ).status == 403


async def test_push_subscriptions_are_bound_to_revocable_devices(settings, push_fixture):
    from app.db.tables import WebPushSubscription
    from tests.test_web_push import subscription

    fixture = push_fixture
    manager = DeviceAuth(fixture.factory, ORIGIN, clock=lambda: fixture.clock[0])
    binding, _, device = await enroll(manager)
    fixture.push.subscribe(subscription(), trusted_device_id=device.id)
    manager.revoke(device.id)
    with fixture.factory() as db:
        row = db.scalar(select(WebPushSubscription))
        assert row.trusted_device_id == device.id and row.disabled_at is not None
    assert manager.state(binding)["state"] == "revoked"
    fixture.clock[0] += 1
    fixture.store.activate(
        peer_id="42",
        peer_type="user",
        thread_id=0,
        is_forum=False,
        title="Чат",
        trigger_id=100,
        preview="Не отправлять",
        reason="private_message",
    )
    await fixture.push.deliver_pending()
    assert fixture.sent == []


async def test_old_unbound_push_is_disabled_on_auth_activation(push_fixture):
    from app.db.tables import WebPushSubscription
    from tests.test_web_push import subscription

    fixture = push_fixture
    fixture.push.subscribe(subscription())
    DeviceAuth(fixture.factory, ORIGIN, clock=lambda: fixture.clock[0])
    with fixture.factory() as db:
        assert db.scalar(select(WebPushSubscription)).disabled_at is not None


@pytest.mark.parametrize(
    "field,value", [("crossOrigin", True), ("topOrigin", "https://evil.example")]
)
async def test_iframe_registration_is_rejected(auth, field, value):
    invite = auth.create_invite()
    ceremony, binding = auth.registration_options(invite["code"], "iPhone")
    credential, _ = registration(ceremony["options"])
    from webauthn import base64url_to_bytes

    client = json.loads(base64url_to_bytes(credential["response"]["clientDataJSON"]))
    client[field] = value
    credential["response"]["clientDataJSON"] = bytes_to_base64url(json.dumps(client).encode())
    with pytest.raises(InboxError):
        await auth.finish_registration(ceremony["ceremony"], credential, binding)


async def test_concurrent_verification_consumes_challenge_once(auth):
    binding, key, _ = await enroll(auth)
    ceremony = auth.login_options(binding)
    credential = assertion(ceremony["options"], key)
    results = await asyncio.gather(
        *[auth.finish_login(ceremony["ceremony"], credential, binding) for _ in range(2)],
        return_exceptions=True,
    )
    assert sum(isinstance(result, str) for result in results) == 1
    assert sum(isinstance(result, InboxError) for result in results) == 1


async def test_authentication_challenge_expires(auth):
    binding, key, _ = await enroll(auth)
    ceremony = auth.login_options(binding)
    credential = assertion(ceremony["options"], key)
    auth.test_clock[0] += 121
    with pytest.raises(InboxError):
        await auth.finish_login(ceremony["ceremony"], credential, binding)


def test_public_listener_cannot_start_without_authentication(service):
    with pytest.raises(ValueError, match="requires authentication"):
        create_app(service, public_auth=True)


@pytest.mark.parametrize(
    "field,value",
    [
        ("inbox_public_origin", "http://detox.example.test"),
        ("inbox_public_origin", "https://detox.example.test/path"),
        ("inbox_public_origin", "https://user@detox.example.test"),
        ("inbox_public_origin", "https://*.example.test"),
        ("inbox_public_origin", "https://45.80.228.215"),
        ("inbox_public_bind_host", "0.0.0.0"),  # noqa: S104 - must be rejected
        ("inbox_public_bind_host", "45.80.228.215"),
    ],
)
def test_public_configuration_rejects_unsafe_origins_and_bindings(settings, field, value):
    from app.config import Settings

    with pytest.raises(ValueError):
        Settings(_env_file=None, **{**settings.model_dump(), field: value})


async def test_push_endpoint_cannot_be_taken_over_by_another_device(push_fixture):
    from tests.test_web_push import subscription

    manager = DeviceAuth(push_fixture.factory, ORIGIN, clock=lambda: push_fixture.clock[0])
    _, _, first = await enroll(manager)
    _, _, second = await enroll(manager)
    push_fixture.push.subscribe(subscription(), trusted_device_id=first.id)
    with pytest.raises(ValueError):
        push_fixture.push.subscribe(subscription(), trusted_device_id=second.id)
    with pytest.raises(ValueError):
        push_fixture.push.unsubscribe(subscription(), trusted_device_id=second.id)


def test_legacy_push_schema_migration_preserves_subscriptions(settings, push_fixture):
    from sqlalchemy import text

    from app.db.tables import WebPushSubscription
    from tests.test_web_push import subscription

    push_fixture.push.subscribe(subscription())
    with push_fixture.factory.begin() as db:
        db.execute(text("DROP INDEX ix_web_push_subscriptions_trusted_device_id"))
        db.execute(text("ALTER TABLE web_push_subscriptions DROP COLUMN trusted_device_id"))
    factory = init_db(settings)
    with factory() as db:
        row = db.scalar(select(WebPushSubscription))
        assert row.endpoint == subscription()["endpoint"] and row.trusted_device_id is None
        assert db.execute(
            text(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND name='ix_web_push_subscriptions_trusted_device_id'"
            )
        ).first()
