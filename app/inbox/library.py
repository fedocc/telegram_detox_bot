from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)
LABEL = re.compile(r"^[^\x00-\x1f\x7f]{1,80}$")


@dataclass(frozen=True)
class LibraryChat:
    id: str
    peer_id: int | str
    title: str
    writable: bool = False


def peer_type_for_marked_id(peer_id: int | str) -> str:
    """Classify a Telethon marked peer ID without exposing it to API callers."""
    try:
        marked = int(peer_id)
    except (TypeError, ValueError):
        return "unknown"
    if marked > 0:
        return "user"
    if marked <= -1_000_000_000_000:
        return "channel"
    return "chat"


def source_token(peer_id: int | str) -> str:
    marked = int(peer_id)
    return f"n{abs(marked)}" if marked < 0 else f"p{marked}"


SAVED_MESSAGES = LibraryChat("saved", "me", "Избранное", True)


def load_library_chats(path: Path) -> tuple[LibraryChat, ...]:
    """Load a small operator-owned allowlist. Invalid rows are skipped safely."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return (SAVED_MESSAGES,)
    except (OSError, UnicodeError, json.JSONDecodeError):
        logger.warning("Library chat configuration is invalid")
        return (SAVED_MESSAGES,)
    rows = raw.get("chats") if isinstance(raw, dict) else raw
    if not isinstance(rows, list):
        logger.warning("Library chat configuration has no chat list")
        return (SAVED_MESSAGES,)
    result, peers = [SAVED_MESSAGES], set()
    for item in rows[:100]:
        if not isinstance(item, dict) or set(item) != {"peer_id", "title"}:
            continue
        title, peer = item.get("title"), item.get("peer_id")
        if not isinstance(title, str) or not LABEL.fullmatch(title.strip()):
            continue
        try:
            peer = int(peer)
        except (TypeError, ValueError):
            continue
        if peer == 0 or peer in peers:
            continue
        peers.add(peer)
        token = source_token(peer)
        result.append(LibraryChat(token, peer, title.strip()))
    return tuple(result)
