from __future__ import annotations

import asyncio
import getpass
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from telethon import TelegramClient, events
from telethon.errors import SessionPasswordNeededError

from app.config import Settings
from app.db import repository
from app.email.sender import EmailSender
from app.ignored_chats import load_ignored_chats_from_settings
from app.services.attention import classify_incoming
from app.services.p0 import handle_p0_candidate
from app.telegram.backfill import run_startup_backfill
from app.telegram.mapper import event_to_stored_message

if TYPE_CHECKING:
    from app.llm.client import HaikuClient

logger = logging.getLogger(__name__)


def make_client(settings: Settings) -> TelegramClient:
    settings.require_telegram_credentials()
    Path(settings.tg_session_path).parent.mkdir(mode=0o700, exist_ok=True)
    return TelegramClient(
        str(settings.tg_session_path),
        settings.tg_api_id,
        settings.tg_api_hash,
        use_ipv6=settings.telegram_use_ipv6,
    )


async def interactive_login(settings: Settings) -> None:
    client = make_client(settings)
    await client.connect()
    if not await client.is_user_authorized():
        await client.send_code_request(settings.tg_phone)
        code = input("Telegram code: ")
        try:
            await client.sign_in(settings.tg_phone, code)
        except SessionPasswordNeededError:
            password = getpass.getpass("Telegram 2FA password: ")
            await client.sign_in(password=password)
    await client.disconnect()
    path = Path(settings.tg_session_path)
    if path.exists():
        os.chmod(path, 0o600)
    print("Telegram session created.")


async def ingest_event(
    event,
    *,
    settings: Settings,
    session_factory,
    llm: HaikuClient | None,
    email: EmailSender,
    ignored_chat_ids: frozenset[str] | set[str],
    inbox=None,
    self_id=None,
    alert_lock=None,
) -> bool:
    if str(event.chat_id) in ignored_chat_ids:
        return False
    if settings.mention_only_mode:
        stored = await event_to_stored_message(event, resolve_reply=False)
    else:
        stored = await event_to_stored_message(event)
    trigger = None
    if settings.mention_only_mode:
        trigger = await classify_incoming(
            getattr(event, "message", None), self_id=self_id,
            text=stored.text or stored.caption, outgoing=stored.is_outgoing,
            sender_id=getattr(event, "sender_id", None),
        )
        if trigger is None:
            return False
        if trigger == "direct_reply":
            stored = stored.model_copy(update={"reply_to_is_mine": True})
    if inbox is not None:
        try:
            await inbox.observe(event, trigger=trigger)
        except Exception as exc:
            logger.warning("Inbox activation failed (%s)", type(exc).__name__)

    def persist_and_alert():
        with session_factory() as session:
            repository.save_message(session, stored)
            if not stored.is_outgoing:
                handle_p0_candidate(
                    session, stored, llm, email, settings=settings,
                    ignored_chat_ids=ignored_chat_ids,
                )

    if alert_lock is None:
        await asyncio.to_thread(persist_and_alert)
    else:
        async with alert_lock:
            await asyncio.to_thread(persist_and_alert)
    return True


async def run_listener(
    settings: Settings,
    session_factory,
    on_connected=None,
    *,
    ignored_chat_ids: frozenset[str] | set[str] | None = None,
    enable_inbox: bool = False,
) -> None:
    if ignored_chat_ids is None:
        ignored_chat_ids = load_ignored_chats_from_settings(settings).chat_ids
    started_at = datetime.now(UTC)
    client = make_client(settings)
    await client.connect()
    if not await client.is_user_authorized():
        await client.disconnect()
        raise RuntimeError("Telegram session is unauthorized. Run telegram_login.")

    me = await client.get_me() if settings.mention_only_mode else None
    logger.info("Telegram connection established; mention_only=%s", settings.mention_only_mode)
    llm = None
    if not settings.mention_only_mode:
        from app.llm.client import HaikuClient

        llm = HaikuClient(settings)
    else:
        logger.info("Mention-only runtime: LLM disabled; startup backfill disabled")
    email = EmailSender(settings)
    if on_connected is not None:
        on_connected(client)

    inbox = None
    if enable_inbox:
        from app.inbox.service import InboxService

        inbox = InboxService(
            client, session_factory,
            lambda: load_ignored_chats_from_settings(settings).chat_ids,
            Path("data/media_cache"), me.id,
        )

    ingestion_lock = asyncio.Lock()

    @client.on(events.NewMessage(incoming=None, outgoing=None))
    async def handler(event) -> None:
        if settings.mention_only_mode and (
            event.out or event.sender_id == me.id or event.date < started_at
        ):
            return
        await ingest_event(
            event, settings=settings, session_factory=session_factory, llm=llm, email=email,
            ignored_chat_ids=(load_ignored_chats_from_settings(settings).chat_ids
                              if inbox else ignored_chat_ids),
            inbox=inbox, self_id=me.id if me else None, alert_lock=ingestion_lock,
        )

    if not settings.mention_only_mode:
        await run_startup_backfill(
            client=client,
            settings=settings,
            session_factory=session_factory,
            llm=llm,
            email_sender=email,
            ignored_chat_ids=ignored_chat_ids,
        )
    if inbox is None:
        await client.run_until_disconnected()
    else:
        from app.inbox.web import serve_inbox

        try:
            async with serve_inbox(inbox):
                await client.run_until_disconnected()
        finally:
            await client.disconnect()
