from __future__ import annotations

import argparse

from app.config import get_settings
from app.models.schemas import ChatType
from app.services.p0 import debug_p0_check


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Check deterministic P0 routing safely.")
    parser.add_argument(
        "--chat-type",
        required=True,
        choices=[ChatType.private.value, ChatType.group.value, ChatType.channel.value],
    )
    parser.add_argument("--text", required=True)
    parser.add_argument("--outgoing", choices=["true", "false"], default="false")
    args = parser.parse_args(argv)
    chat_type = ChatType(args.chat_type)
    is_outgoing = args.outgoing == "true"
    try:
        result = debug_p0_check(
            chat_type,
            args.text,
            get_settings(),
            is_outgoing=is_outgoing,
        )
    except Exception:
        result = {
            "is_p0": False,
            "reason_category": "configuration_error",
            "matched_signal": "none",
            "chat_type": chat_type.value,
            "is_outgoing": is_outgoing,
        }
    print(f"is_p0={str(result['is_p0']).lower()}")
    print(f"reason_category={result['reason_category']}")
    print(f"matched_signal={result['matched_signal']}")
    print(f"chat_type={result['chat_type']}")
    print(f"is_outgoing={str(result['is_outgoing']).lower()}")


if __name__ == "__main__":
    main()
