from __future__ import annotations

import re


def has_exact_fedocc_mention(text: str | None) -> bool:
    """Match only the standalone Telegram username required by mention-only mode."""
    return bool(
        text
        and re.search(r"(?<![A-Za-z0-9_])@fedocc(?![A-Za-z0-9_])", text, re.IGNORECASE)
    )
