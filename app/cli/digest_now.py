from __future__ import annotations

import argparse
from collections.abc import Callable
from datetime import datetime
from zoneinfo import ZoneInfo

from app.config import get_settings
from app.db.session import init_db
from app.email.sender import EmailSender
from app.ignored_chats import load_ignored_chats_from_settings
from app.llm.client import (
    HaikuClient,
    sanitize_validation_codes,
    sanitize_validation_error_type,
    sanitize_validation_paths,
)
from app.services.digest import generate_digest, send_daily_digest_pipeline


def run(
    *,
    dry_run: bool = False,
    llm_debug: bool = False,
    settings=None,
    session_factory=None,
    llm=None,
    email_sender=None,
    now: datetime | None = None,
    output: Callable[[str], None] = print,
):
    settings = settings or get_settings()
    ignored_chat_ids = load_ignored_chats_from_settings(settings).chat_ids
    session_factory = session_factory or init_db(settings)
    now = now or datetime.now(ZoneInfo(settings.timezone))
    llm = llm or HaikuClient(settings)
    with session_factory() as session:
        if dry_run:
            digest = generate_digest(
                session,
                llm,
                now.date(),
                settings.timezone,
                ignored_chat_ids=ignored_chat_ids,
            )
        else:
            digest = send_daily_digest_pipeline(
                session,
                llm,
                email_sender or EmailSender(settings),
                now.date(),
                settings.timezone,
                ignored_chat_ids=ignored_chat_ids,
            )
    if dry_run:
        diagnostics = digest.diagnostics
        safe_error_type = sanitize_validation_error_type(
            diagnostics.validation_error_type
        )
        safe_paths = sanitize_validation_paths(diagnostics.validation_error_paths)
        safe_codes = sanitize_validation_codes(diagnostics.validation_error_codes)
        output("Dry-run: digest would be generated")
        output("Dry-run: digest not sent")
        output(f"chats_count={diagnostics.chats_count}")
        output(f"messages_count={diagnostics.messages_count}")
        output(f"llm_attempted={str(diagnostics.llm_attempted).lower()}")
        output(f"llm_used={str(diagnostics.llm_used).lower()}")
        output(f"fallback_reason={diagnostics.fallback_reason or 'none'}")
        output(f"validation_error_type={safe_error_type or 'none'}")
        output("validation_error_paths=" + (",".join(safe_paths) or "none"))
        output("validation_error_codes=" + (",".join(safe_codes) or "none"))
        output(f"repair_attempted={str(diagnostics.repair_attempted).lower()}")
        output(f"repair_used={str(diagnostics.repair_used).lower()}")
        if llm_debug:
            output(f"expected_chat_count={diagnostics.expected_chat_count}")
            output(f"returned_chat_count={diagnostics.returned_chat_count}")
            output(f"missing_chat_count={diagnostics.missing_chat_count}")
            output(f"duplicate_chat_count={diagnostics.duplicate_chat_count}")
            output(f"unknown_chat_count={diagnostics.unknown_chat_count}")
    else:
        output("Digest sent.")
    return digest


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Generate the Telegram daily digest")
    parser.add_argument("--dry-run", action="store_true", help="generate without sending email")
    parser.add_argument(
        "--llm-debug",
        action="store_true",
        help="print safe LLM validation counters (never raw output)",
    )
    args = parser.parse_args(argv)
    run(dry_run=args.dry_run, llm_debug=args.llm_debug)


if __name__ == "__main__":
    main()
