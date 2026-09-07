from __future__ import annotations

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
from app.services.p0 import handle_p0_candidate, has_exact_fedocc_mention
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
) -> bool:
    if str(event.chat_id) in ignored_chat_ids:
        return False
    if settings.mention_only_mode:
        stored = await event_to_stored_message(event, resolve_reply=False)
    else:
        stored = await event_to_stored_message(event)
    if settings.mention_only_mode and (
        stored.is_outgoing or not has_exact_fedocc_mention(stored.text or stored.caption)
    ):
        return False
    with session_factory() as session:
        repository.save_message(session, stored)
        if not stored.is_outgoing:
            handle_p0_candidate(
                session,
                stored,
                llm,
                email,
                settings=settings,
                ignored_chat_ids=ignored_chat_ids,
            )
    return True


async def run_listener(
    settings: Settings,
    session_factory,
    on_connected=None,
    *,
    ignored_chat_ids: frozenset[str] | set[str] | None = None,
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

    @client.on(events.NewMessage(incoming=None, outgoing=None))
    async def handler(event) -> None:
        if settings.mention_only_mode and (
            event.out or event.sender_id == me.id or event.date < started_at
        ):
            return
        await ingest_event(
            event,
            settings=settings,
            session_factory=session_factory,
            llm=llm,
            email=email,
            ignored_chat_ids=ignored_chat_ids,
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
    await client.run_until_disconnected()
