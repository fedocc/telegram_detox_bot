from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app.telegram import client as telegram_client


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
    forbidden = {
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
    root = Path(__file__).resolve().parents[1]
    offenders: list[tuple[str, str]] = []
    for path in (root / "app").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in forbidden:
                offenders.append((path.relative_to(root).as_posix(), node.attr))

    assert offenders == []
