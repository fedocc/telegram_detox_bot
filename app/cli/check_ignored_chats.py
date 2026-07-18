from __future__ import annotations

from collections.abc import Callable

from app.config import Settings, get_settings
from app.ignored_chats import IgnoredChatsConfigError, load_ignored_chats_from_settings


def run(settings: Settings, *, output: Callable[[str], None] = print) -> int:
    try:
        config = load_ignored_chats_from_settings(settings)
    except IgnoredChatsConfigError as exc:
        output(f"ERROR: {exc}")
        return 1
    output(f"Ignored chats: {len(config.chat_ids)}")
    if config.invalid_id_count:
        output(f"WARNING: invalid chat IDs skipped: {config.invalid_id_count}")
    return 0


def main() -> None:
    raise SystemExit(run(get_settings()))


if __name__ == "__main__":
    main()
