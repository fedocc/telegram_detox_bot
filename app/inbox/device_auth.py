"""Passkeys plus an independently approved browser binding; no public administrator."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import io
import ipaddress
import json
import re
import secrets
import time
from collections import OrderedDict
from urllib.parse import urlsplit
from uuid import uuid4

import qrcode
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.exc import IntegrityError
from webauthn import (
    base64url_to_bytes,
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers.cose import COSEAlgorithmIdentifier
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from app.db.tables import (
    InboxAuthAudit,
    InboxAuthChallenge,
    InboxDeviceInvite,
    InboxDeviceSession,
    InboxTrustedDevice,
    WebPushSubscription,
)
from app.inbox.errors import InboxError

DEVICE_COOKIE = "__Host-td_device"
SESSION_COOKIE = "__Host-td_session"
ENROLL_COOKIE = "__Host-td_enroll"
TRUST_SECONDS = 180 * 86400
SESSION_SECONDS = 12 * 3600
CHALLENGE_SECONDS = 120
INVITE_SECONDS = 300
SECRET = re.compile(r"^[A-Za-z0-9_-]{43}$")
IDENTIFIER = re.compile(r"^[a-f0-9]{32}$")
ALGORITHMS = [
    COSEAlgorithmIdentifier.ECDSA_SHA_256,
    COSEAlgorithmIdentifier.RSASSA_PKCS1_v1_5_SHA_256,
]


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def valid_secret(value):
    return isinstance(value, str) and SECRET.fullmatch(value) is not None


class DeviceAuth:
    def __init__(self, factory, origin, *, clock=time.time, proxy_ip=""):
        parsed = urlsplit(origin)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.path
            or parsed.query
            or parsed.fragment
            or parsed.port not in {None, 443}
        ):
            raise ValueError("Public access requires an exact HTTPS origin")
        self.factory, self.origin, self.rp_id, self.clock = factory, origin, parsed.hostname, clock
        self.proxy_ip = str(ipaddress.ip_address(proxy_ip)) if proxy_ip else ""
        self.limits = OrderedDict()
        self.active = {}
        self.crypto_slots = asyncio.Semaphore(2)
        # Pre-authentication subscriptions cannot survive activation of device access.
        with self.factory() as db:
            db.execute(
                update(WebPushSubscription)
                .where(
                    WebPushSubscription.trusted_device_id.is_(None),
                    WebPushSubscription.disabled_at.is_(None),
                )
                .values(disabled_at=self.clock())
            )
            db.commit()

    def ip(self, request):
        candidate = (
            request.headers.get("X-Real-IP", "")
            if request.remote == self.proxy_ip
            else request.remote
        )
        try:
            return str(ipaddress.ip_address(candidate or ""))
        except ValueError:
            return ""

    def rate_limit(self, ip, action):
        now = self.clock()
        for key, maximum in ((("global", action), 60), ((ip, action), 12)):
            start, count = self.limits.get(key, (now, 0))
            if now - start >= 60:
                start, count = now, 0
            if count >= maximum:
                raise InboxError("Слишком много попыток. Подождите минуту.", 429, retry_after=60)
            self.limits[key] = (start, count + 1)
            self.limits.move_to_end(key)
        while len(self.limits) > 4096:
            self.limits.popitem(last=False)

    @staticmethod
    def _transaction(db):
        if db.get_bind().dialect.name == "sqlite":
            db.execute(text("BEGIN IMMEDIATE"))

    def _audit(self, db, event, device=None, ip=""):
        record = InboxAuthAudit(
            event=event,
            device_id=device.id if device else None,
            label=device.label if device else "",
            ip=ip[:45],
            created_at=self.clock(),
        )
        db.add(record)
        db.flush()
        db.execute(
            delete(InboxAuthAudit).where(
                (InboxAuthAudit.id <= record.id - 1000)
                | (InboxAuthAudit.created_at < self.clock() - 30 * 86400)
            )
        )

    def audit_failure(self, event, ip):
        with self.factory() as db:
            self._audit(db, event, ip=ip)
            db.commit()

    def record_rejection(self, ip):
        try:
            self.rate_limit(ip, "audit")
        except InboxError:
            return
        self.audit_failure("auth_rejected", ip)

    def _cleanup(self, db):
        now = self.clock()
        db.execute(
            update(InboxTrustedDevice)
            .where(
                InboxTrustedDevice.approved_at.is_(None),
                InboxTrustedDevice.revoked_at.is_(None),
                InboxTrustedDevice.created_at < now - 900,
            )
            .values(revoked_at=now)
        )
        db.execute(delete(InboxAuthChallenge).where(InboxAuthChallenge.expires_at <= now))
        db.execute(delete(InboxDeviceSession).where(InboxDeviceSession.expires_at <= now))
        db.execute(delete(InboxDeviceInvite).where(InboxDeviceInvite.expires_at < now - 3600))

    def _device(self, db, binding):
        if not valid_secret(binding):
            return None
        return db.scalar(
            select(InboxTrustedDevice).where(InboxTrustedDevice.binding_hash == digest(binding))
        )

    def _approved(self, device):
        return (
            device is not None
            and device.approved_at is not None
            and device.revoked_at is None
            and device.trusted_until > self.clock()
        )

    def state(self, binding):
        with self.factory() as db:
            device = self._device(db, binding)
            if device is None:
                return {"state": "new"}
            state = (
                "revoked"
                if device.revoked_at is not None
                else "expired"
                if device.approved_at is None and device.created_at < self.clock() - 900
                else "pending"
                if device.approved_at is None
                else "approved"
                if self._approved(device)
                else "expired"
            )
            return {
                "state": state,
                "label": device.label,
                "code": device.confirmation_code if state == "pending" else "",
            }

    def authenticate(self, binding, token, *, ip=""):
        if not valid_secret(binding) or not valid_secret(token):
            return None
        now = self.clock()
        with self.factory() as db:
            session = db.get(InboxDeviceSession, digest(token))
            device = self._device(db, binding)
            if (
                session is None
                or not self._approved(device)
                or session.device_id != device.id
                or session.expires_at <= now
            ):
                return None
            if now - session.last_used_at >= 60:
                session.last_used_at = now
                device.last_used_at, device.last_ip = now, ip
                db.commit()
            return {
                "device_id": device.id,
                "csrf": session.csrf,
                "label": device.label,
                "expires_at": session.expires_at,
            }

    def create_invite(self):
        token, identifier, now = secrets.token_urlsafe(32), uuid4().hex, self.clock()
        manual_code = f"{secrets.randbelow(10**12):012}"
        with self.factory() as db:
            self._transaction(db)
            self._cleanup(db)
            count = db.scalar(
                select(func.count())
                .select_from(InboxDeviceInvite)
                .where(
                    InboxDeviceInvite.expires_at > now,
                    InboxDeviceInvite.consumed_at.is_(None),
                    InboxDeviceInvite.revoked_at.is_(None),
                )
            )
            if count >= 3:
                raise InboxError("Уже есть три приглашения. Дождитесь их окончания.", 429)
            db.add(
                InboxDeviceInvite(
                    id=identifier,
                    token_hash=digest(token),
                    manual_hash=digest(manual_code),
                    created_at=now,
                    expires_at=now + INVITE_SECONDS,
                )
            )
            self._audit(db, "invitation_created")
            db.commit()
        url = self.origin + "/login#pair=" + token
        image = qrcode.make(url)
        output = io.BytesIO()
        image.save(output, format="PNG")
        return {
            "id": identifier,
            "url": url,
            "code": manual_code,
            "expires_at": now + INVITE_SECONDS,
            "qr": base64.b64encode(output.getvalue()).decode(),
        }

    def cancel_invite(self, identifier):
        with self.factory() as db:
            invite = db.get(InboxDeviceInvite, identifier)
            if invite:
                invite.revoked_at = self.clock()
                self._audit(db, "invitation_cancelled")
                db.commit()

    def _invitation(self, db, token):
        manual = isinstance(token, str) and re.fullmatch(r"[0-9]{12}", token) is not None
        if not valid_secret(token) and not manual:
            raise InboxError("Приглашение недоступно. Создайте новое на Mac.", 403)
        column = InboxDeviceInvite.manual_hash if manual else InboxDeviceInvite.token_hash
        invite = db.scalar(select(InboxDeviceInvite).where(column == digest(token)))
        if (
            invite is None
            or invite.expires_at <= self.clock()
            or invite.consumed_at is not None
            or invite.revoked_at is not None
        ):
            raise InboxError("Приглашение недоступно. Создайте новое на Mac.", 403)
        return invite

    def registration_options(self, invitation, label, *, ip=""):
        self.rate_limit(ip, "registration")
        if not isinstance(label, str) or not 1 <= len(label.strip()) <= 64:
            raise InboxError("Укажите название устройства, до 64 символов.")
        label = " ".join(label.split())
        now, binding = self.clock(), secrets.token_urlsafe(32)
        user_handle, device_id, challenge_id = secrets.token_bytes(32), uuid4().hex, uuid4().hex
        options = generate_registration_options(
            rp_id=self.rp_id,
            rp_name="Telegram Detox",
            user_id=user_handle,
            user_name="device-" + device_id,
            user_display_name=label,
            supported_pub_key_algs=ALGORITHMS,
            authenticator_selection=AuthenticatorSelectionCriteria(
                resident_key=ResidentKeyRequirement.PREFERRED,
                user_verification=UserVerificationRequirement.REQUIRED,
            ),
            timeout=120000,
        )
        with self.factory() as db:
            self._transaction(db)
            self._cleanup(db)
            invite = self._invitation(db, invitation)
            count = db.scalar(
                select(func.count())
                .select_from(InboxTrustedDevice)
                .where(
                    InboxTrustedDevice.revoked_at.is_(None),
                    (
                        InboxTrustedDevice.approved_at.is_(None)
                        | (InboxTrustedDevice.trusted_until > now)
                    ),
                )
            )
            if count >= 10:
                raise InboxError("Достигнут предел устройств. Удалите старое на Mac.", 409)
            db.add(
                InboxAuthChallenge(
                    id=challenge_id,
                    kind="registration",
                    challenge=options.challenge,
                    device_id=device_id,
                    binding_hash=digest(binding),
                    invitation_id=invite.id,
                    user_handle=user_handle,
                    label=label,
                    created_at=now,
                    expires_at=min(now + CHALLENGE_SECONDS, invite.expires_at),
                )
            )
            db.commit()
        return {"ceremony": challenge_id, "options": json.loads(options_to_json(options))}, binding

    def _challenge(self, db, identifier, kind, binding):
        if (
            not isinstance(identifier, str)
            or not IDENTIFIER.fullmatch(identifier)
            or not valid_secret(binding)
        ):
            raise InboxError("Подтверждение устарело. Повторите вход.", 403)
        challenge = db.get(InboxAuthChallenge, identifier)
        if (
            challenge is None
            or challenge.kind != kind
            or challenge.used_at is not None
            or challenge.expires_at <= self.clock()
            or not hmac.compare_digest(challenge.binding_hash, digest(binding))
        ):
            raise InboxError("Подтверждение устарело. Повторите вход.", 403)
        return challenge

    @staticmethod
    def _top_level(credential):
        client = json.loads(base64url_to_bytes(credential["response"]["clientDataJSON"]))
        if client.get("crossOrigin", False) is not False or client.get("topOrigin") is not None:
            raise ValueError("Cross-origin credentials are not accepted")

    async def finish_registration(self, identifier, credential, binding, *, ip=""):
        self.rate_limit(ip, "verification")
        with self.factory() as db:
            challenge = self._challenge(db, identifier, "registration", binding)
        try:
            self._top_level(credential)
            async with self.crypto_slots, asyncio.timeout(20):
                verified = await asyncio.to_thread(
                    verify_registration_response,
                    credential=credential,
                    expected_challenge=challenge.challenge,
                    expected_rp_id=self.rp_id,
                    expected_origin=self.origin,
                    require_user_verification=True,
                    supported_pub_key_algs=ALGORITHMS,
                )
        except Exception:
            self.audit_failure("registration_rejected", ip)
            raise InboxError(
                "Не удалось подтвердить ключ доступа. Повторите подключение.", 403
            ) from None
        try:
            with self.factory() as db:
                self._transaction(db)
                challenge = self._challenge(db, identifier, "registration", binding)
                invite = db.get(InboxDeviceInvite, challenge.invitation_id)
                if (
                    invite is None
                    or invite.expires_at <= self.clock()
                    or invite.consumed_at is not None
                    or invite.revoked_at is not None
                ):
                    raise InboxError("Приглашение недоступно. Создайте новое на Mac.", 403)
                count = db.scalar(
                    select(func.count())
                    .select_from(InboxTrustedDevice)
                    .where(
                        InboxTrustedDevice.revoked_at.is_(None),
                        (
                            InboxTrustedDevice.approved_at.is_(None)
                            | (InboxTrustedDevice.trusted_until > self.clock())
                        ),
                    )
                )
                if count >= 10:
                    raise InboxError("Достигнут предел устройств.", 409)
                device = InboxTrustedDevice(
                    id=challenge.device_id,
                    label=challenge.label,
                    binding_hash=challenge.binding_hash,
                    user_handle=challenge.user_handle,
                    credential_id=verified.credential_id,
                    credential_key=verified.credential_public_key,
                    sign_count=verified.sign_count,
                    backup_eligible=verified.credential_device_type.value == "multi_device",
                    created_at=self.clock(),
                    confirmation_code=f"{secrets.randbelow(1000000):06}",
                    last_ip=ip,
                )
                db.add(device)
                challenge.used_at = invite.consumed_at = self.clock()
                self._audit(db, "device_pending", device, ip)
                db.commit()
        except IntegrityError:
            raise InboxError("Этот ключ уже зарегистрирован.", 409) from None
        return self.state(binding)

    def approve(self, identifier, code):
        with self.factory() as db:
            self._transaction(db)
            device = db.get(InboxTrustedDevice, identifier)
            if (
                device is None
                or device.revoked_at is not None
                or device.approved_at is not None
                or device.created_at < self.clock() - 900
                or not isinstance(code, str)
                or not hmac.compare_digest(code, device.confirmation_code)
            ):
                raise InboxError("Сверьте код на телефоне и Mac. Запрос мог устареть.", 409)
            device.approved_at, device.trusted_until = self.clock(), self.clock() + TRUST_SECONDS
            self._audit(db, "device_approved", device)
            db.commit()

    def login_options(self, binding, *, ip=""):
        self.rate_limit(ip, "login")
        with self.factory() as db:
            self._transaction(db)
            self._cleanup(db)
            device = self._device(db, binding)
            if not self._approved(device):
                raise InboxError("Добавьте этот браузер через приглашение на Mac.", 401)
            options = generate_authentication_options(
                rp_id=self.rp_id,
                allow_credentials=[PublicKeyCredentialDescriptor(id=device.credential_id)],
                user_verification=UserVerificationRequirement.REQUIRED,
                timeout=120000,
            )
            identifier = uuid4().hex
            db.add(
                InboxAuthChallenge(
                    id=identifier,
                    kind="login",
                    challenge=options.challenge,
                    device_id=device.id,
                    binding_hash=device.binding_hash,
                    user_handle=device.user_handle,
                    created_at=self.clock(),
                    expires_at=self.clock() + CHALLENGE_SECONDS,
                )
            )
            db.commit()
        return {"ceremony": identifier, "options": json.loads(options_to_json(options))}

    async def finish_login(self, identifier, credential, binding, *, ip=""):
        self.rate_limit(ip, "verification")
        with self.factory() as db:
            challenge = self._challenge(db, identifier, "login", binding)
            device = self._device(db, binding)
            if not self._approved(device) or challenge.device_id != device.id:
                raise InboxError("Устройство не подтверждено.", 401)
        try:
            self._top_level(credential)
            handle = credential.get("response", {}).get("userHandle")
            if handle is not None and not hmac.compare_digest(
                base64url_to_bytes(handle), device.user_handle
            ):
                raise ValueError("User handle mismatch")
            async with self.crypto_slots, asyncio.timeout(20):
                verified = await asyncio.to_thread(
                    verify_authentication_response,
                    credential=credential,
                    expected_challenge=challenge.challenge,
                    expected_rp_id=self.rp_id,
                    expected_origin=self.origin,
                    credential_public_key=device.credential_key,
                    credential_current_sign_count=device.sign_count,
                    require_user_verification=True,
                )
            if (
                verified.credential_id != device.credential_id
                or (verified.credential_device_type.value == "multi_device")
                != device.backup_eligible
            ):
                raise ValueError("Credential mismatch")
        except Exception:
            self.audit_failure("login_rejected", ip)
            raise InboxError("Ключ доступа не подошёл. Повторите вход.", 403) from None
        with self.factory() as db:
            self._transaction(db)
            challenge = self._challenge(db, identifier, "login", binding)
            device = self._device(db, binding)
            if not self._approved(device) or challenge.device_id != device.id:
                raise InboxError("Устройство не подтверждено.", 401)
            if (
                verified.new_sign_count or device.sign_count
            ) and verified.new_sign_count <= device.sign_count:
                raise InboxError("Подтверждение уже использовано. Повторите вход.", 403)
            challenge.used_at = self.clock()
            device.sign_count = verified.new_sign_count
            device.last_used_at, device.last_ip = self.clock(), ip
            db.execute(delete(InboxDeviceSession).where(InboxDeviceSession.device_id == device.id))
            token = secrets.token_urlsafe(32)
            db.add(
                InboxDeviceSession(
                    token_hash=digest(token),
                    device_id=device.id,
                    csrf=secrets.token_urlsafe(32),
                    created_at=self.clock(),
                    expires_at=min(self.clock() + SESSION_SECONDS, device.trusted_until),
                    last_used_at=self.clock(),
                )
            )
            self._audit(db, "login_succeeded", device, ip)
            db.commit()
        return token

    def logout(self, binding, token, *, ip=""):
        removed, identifier = False, None
        with self.factory() as db:
            device = self._device(db, binding)
            if device and valid_secret(token):
                result = db.execute(
                    delete(InboxDeviceSession).where(
                        InboxDeviceSession.token_hash == digest(token),
                        InboxDeviceSession.device_id == device.id,
                    )
                )
                removed, identifier = result.rowcount > 0, device.id
                if removed:
                    self._audit(db, "logout", device, ip)
                db.commit()
        if removed:
            for task in tuple(self.active.pop(identifier, ())):
                task.cancel()

    def revoke(self, identifier):
        with self.factory() as db:
            self._transaction(db)
            device = db.get(InboxTrustedDevice, identifier)
            if device is None:
                raise InboxError("Устройство не найдено.", 404)
            device.revoked_at = self.clock()
            db.execute(
                update(WebPushSubscription)
                .where(WebPushSubscription.trusted_device_id == device.id)
                .values(disabled_at=self.clock())
            )
            db.execute(delete(InboxDeviceSession).where(InboxDeviceSession.device_id == device.id))
            self._audit(db, "device_revoked", device)
            db.commit()
        for task in tuple(self.active.pop(identifier, ())):
            task.cancel()

    def overview(self):
        with self.factory() as db:
            devices = list(
                db.scalars(
                    select(InboxTrustedDevice)
                    .order_by(InboxTrustedDevice.created_at.desc())
                    .limit(50)
                )
            )
            events = list(
                db.scalars(select(InboxAuthAudit).order_by(InboxAuthAudit.id.desc()).limit(50))
            )
            return {
                "origin": self.origin,
                "devices": [
                    {
                        "id": d.id,
                        "label": d.label,
                        "created_at": d.created_at,
                        "approved_at": d.approved_at,
                        "trusted_until": d.trusted_until,
                        "revoked_at": d.revoked_at,
                        "last_used_at": d.last_used_at,
                        "last_ip": d.last_ip,
                        "code": d.confirmation_code
                        if d.approved_at is None and d.revoked_at is None
                        else "",
                    }
                    for d in devices
                ],
                "events": [
                    {
                        "id": e.id,
                        "event": e.event,
                        "label": e.label,
                        "ip": e.ip,
                        "created_at": e.created_at,
                    }
                    for e in events
                ],
            }
