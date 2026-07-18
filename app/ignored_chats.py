from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.config import Settings

CHAT_ID_RE = re.compile(r"-?[1-9]\d*")


class IgnoredChatsConfigError(ValueError):
    """Raised when the private ignored-chat file cannot be trusted."""


@dataclass(frozen=True, slots=True)
class IgnoredChatsConfig:
    chat_ids: frozenset[str]
    invalid_id_count: int = 0

    def contains(self, chat_id: str | int | None) -> bool:
        return chat_id is not None and str(chat_id) in self.chat_ids


def _normalize_chat_id(value: object) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    normalized = str(value).strip()
    if not CHAT_ID_RE.fullmatch(normalized):
        return None
    return normalized


def _env_chat_ids(raw_ids: str) -> tuple[set[str], int]:
    chat_ids: set[str] = set()
    invalid_count = 0
    for raw_id in raw_ids.split(","):
        if not raw_id.strip():
            continue
        normalized = _normalize_chat_id(raw_id)
        if normalized is None:
            invalid_count += 1
        else:
            chat_ids.add(normalized)
    return chat_ids, invalid_count


def _file_chat_ids(path: Path) -> tuple[set[str], int]:
    if not path.exists():
        return set(), 0
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise IgnoredChatsConfigError(
            "Invalid ignored chats configuration: expected readable JSON."
        ) from exc
    if not isinstance(payload, list):
        raise IgnoredChatsConfigError(
            "Invalid ignored chats configuration: top-level value must be a JSON array."
        )

    chat_ids: set[str] = set()
    invalid_count = 0
    for index, item in enumerate(payload):
        if not isinstance(item, dict) or "chat_id" not in item:
            raise IgnoredChatsConfigError(
                f"Invalid ignored chats configuration: entry {index + 1} must contain chat_id."
            )
        reason = item.get("reason")
        if reason is not None and not isinstance(reason, str):
            raise IgnoredChatsConfigError(
                f"Invalid ignored chats configuration: entry {index + 1} has invalid reason."
            )
        normalized = _normalize_chat_id(item["chat_id"])
        if normalized is None:
            invalid_count += 1
        else:
            chat_ids.add(normalized)
    return chat_ids, invalid_count


def load_ignored_chats(
    env_ids: str = "",
    path: Path = Path("data/ignored_chats.json"),
) -> IgnoredChatsConfig:
    env_chat_ids, env_invalid = _env_chat_ids(env_ids)
    file_chat_ids, file_invalid = _file_chat_ids(path)
    return IgnoredChatsConfig(
        chat_ids=frozenset(env_chat_ids | file_chat_ids),
        invalid_id_count=env_invalid + file_invalid,
    )


def load_ignored_chats_from_settings(settings: Settings) -> IgnoredChatsConfig:
    return load_ignored_chats(settings.ignore_chat_ids, settings.ignored_chats_path)
