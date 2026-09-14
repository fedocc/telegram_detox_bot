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


SAVED_MESSAGES = LibraryChat("saved", "me", "Сохранённые сообщения", True)


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
        token = f"n{abs(peer)}" if peer < 0 else f"p{peer}"
        result.append(LibraryChat(token, peer, title.strip()))
    return tuple(result)
