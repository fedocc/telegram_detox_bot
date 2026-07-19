from __future__ import annotations

import argparse
from pathlib import Path

from app.ignored_chats import IgnoredChatsConfigError, add_ignored_chat


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Add one chat to the local ignore list safely.")
    parser.add_argument("--chat-id", required=True)
    parser.add_argument("--reason", required=True)
    parser.add_argument("--path", type=Path, default=Path("data/ignored_chats.json"))
    args = parser.parse_args(argv)
    try:
        added = add_ignored_chat(args.path, args.chat_id, args.reason)
    except IgnoredChatsConfigError as exc:
        parser.error(str(exc))
    print(f"ignored_chat_added={str(added).lower()}")


if __name__ == "__main__":
    main()
