from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app.telegram import client as telegram_client

BANNED_WRITES = {
    "send_message",
    "forward_messages",
    "delete_messages",
    "edit_message",
    "mark_read",
    "send_reaction",
    "react",
    "join_channel",
    "leave_channel",
    "archive",
    "mute",
    "pin_message",
    "unpin_message",
}


def scan_telegram_safety_source(source: str, path: str = "snippet.py") -> list[tuple[str, str]]:
    tree = ast.parse(source)
    offenders: list[tuple[str, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in BANNED_WRITES:
            offenders.append((path, node.attr))
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in {"getattr", "setattr"}:
                target = node.args[0] if node.args else None
                attr = node.args[1] if len(node.args) > 1 else None
                target_name = target.id if isinstance(target, ast.Name) else ""
                attr_name = attr.value if isinstance(attr, ast.Constant) else ""
                if target_name in {"client", "telegram_client"} or attr_name in BANNED_WRITES:
                    offenders.append((path, func.id))
            if isinstance(func, ast.Attribute) and func.attr in {"__getattribute__", "__getattr__"}:
                offenders.append((path, func.attr))
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value in BANNED_WRITES
        ):
            offenders.append((path, node.value))
    return offenders


def scan_telegram_safety_tree(root: Path) -> list[tuple[str, str]]:
    offenders: list[tuple[str, str]] = []
    for path in (root / "app").rglob("*.py"):
        offenders.extend(scan_telegram_safety_source(path.read_text(encoding="utf-8"), str(path)))
    return offenders


class FakeClient:
    def __init__(self, authorized: bool) -> None:
        self.authorized = authorized
        self.connected = False
        self.started = False
        self.handlers = []

    def on(self, event):
        def decorator(fn):
            self.handlers.append((event, fn))
            return fn

        return decorator

    async def connect(self) -> None:
        self.connected = True

    async def is_user_authorized(self) -> bool:
        return self.authorized

    async def start(self, *args, **kwargs) -> None:
        self.started = True

    async def run_until_disconnected(self) -> None:
        return None

    async def disconnect(self) -> None:
        return None


def test_runtime_listener_does_not_login_when_session_missing(monkeypatch, settings) -> None:
    fake = FakeClient(authorized=False)
    monkeypatch.setattr(telegram_client, "make_client", lambda settings: fake)

    with pytest.raises(RuntimeError, match="Telegram session is unauthorized"):
        import asyncio

        asyncio.run(telegram_client.run_listener(settings, lambda: None))

    assert fake.started is False


def test_runtime_listener_fails_closed_when_unauthorized(monkeypatch, settings) -> None:
    fake = FakeClient(authorized=False)
    monkeypatch.setattr(telegram_client, "make_client", lambda settings: fake)

    with pytest.raises(RuntimeError, match="Run telegram_login"):
        import asyncio

        asyncio.run(telegram_client.run_listener(settings, lambda: None))

    assert fake.connected is True


def test_only_telegram_login_cli_can_call_start() -> None:
    root = Path(__file__).resolve().parents[1]
    offenders = []
    for path in (root / "app").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == "start":
                offenders.append(path.relative_to(root).as_posix())

    assert offenders == []


def test_no_telegram_write_methods_used_in_runtime_code() -> None:
    root = Path(__file__).resolve().parents[1]
    assert scan_telegram_safety_tree(root) == []


def test_no_dynamic_telegram_dispatch_in_runtime_code() -> None:
    root = Path(__file__).resolve().parents[1]
    assert scan_telegram_safety_tree(root) == []


def test_safety_scan_detects_getattr_write_attempt() -> None:
    offenders = scan_telegram_safety_source('getattr(client, "send_message")("x")')

    assert offenders


def test_safety_scan_detects_direct_write_attempt() -> None:
    offenders = scan_telegram_safety_source("client.send_message('x')")

    assert offenders


def test_scanner_covers_full_app_tree(tmp_path) -> None:
    evil = tmp_path / "app" / "services" / "evil.py"
    evil.parent.mkdir(parents=True)
    evil.write_text("client.send_message('x')", encoding="utf-8")

    assert scan_telegram_safety_tree(tmp_path)


def test_scanner_detects_direct_write_outside_telegram_folder(tmp_path) -> None:
    evil = tmp_path / "app" / "services" / "evil.py"
    evil.parent.mkdir(parents=True)
    evil.write_text("client.delete_messages(1)", encoding="utf-8")

    assert scan_telegram_safety_tree(tmp_path)


def test_scanner_detects_getattr_write_outside_telegram_folder(tmp_path) -> None:
    evil = tmp_path / "app" / "services" / "evil.py"
    evil.parent.mkdir(parents=True)
    evil.write_text('getattr(client, "send_message")("x")', encoding="utf-8")

    assert scan_telegram_safety_tree(tmp_path)


def test_scanner_detects_dunder_dynamic_dispatch_outside_telegram_folder(tmp_path) -> None:
    evil = tmp_path / "app" / "services" / "evil.py"
    evil.parent.mkdir(parents=True)
    evil.write_text('client.__getattribute__("delete_messages")(1)', encoding="utf-8")

    assert scan_telegram_safety_tree(tmp_path)


def test_scanner_detects_string_based_banned_method_lookup(tmp_path) -> None:
    evil = tmp_path / "app" / "services" / "evil.py"
    evil.parent.mkdir(parents=True)
    evil.write_text(
        'method_name = "forward_messages"\ngetattr(client, method_name)',
        encoding="utf-8",
    )

    assert scan_telegram_safety_tree(tmp_path)
