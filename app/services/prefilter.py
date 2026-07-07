from __future__ import annotations

import re

P0_PATTERNS = [
    r"\bсрочно\b",
    r"\burgent\b",
    r"\bпозвони\b",
    r"\bнабери\b",
    r"\bподключайся\b",
    r"\bсозвон\b",
    r"\bчерез час\b",
    r"\bпрямо сейчас\b",
    r"\bсегодня до\b",
    r"\bдо\s+\d{1,2}[:.]\d{2}\b",
    r"\bдо\s+\d{1,2}\b",
    r"\bсегодня до\s+\d{1,2}[:.]\d{2}\b",
    r"\bзавтра в\s+\d{1,2}[:.]\d{2}\b",
    r"\bты где\b",
    r"\bнабери меня\b",
    r"\bперезвони\b",
    r"\bдедлайн\b",
    r"\bнужно сегодня\b",
    r"\basap\b",
    r"\bas soon as possible\b",
    r"\bdeadline in \d+ hours?\b",
    r"\bчерез \d+ (минут|час|часа|часов)\b",
]
P0_RE = re.compile("|".join(P0_PATTERNS), re.IGNORECASE)

URGENT_CALL_RE = re.compile(
    "|".join([
        r"\bcall(?:\s+me)?(?:\s+back)?\b",
        r"\bphone\b",
        r"\bjoin(?:\s+the)?\s+call\b",
        r"\bconnect\b",
        r"\bпозвони\b",
        r"\bперезвони\b",
        r"\bнабери\b",
        r"\bподключ\w*\b",
        r"\bсозвон\b",
    ]),
    re.IGNORECASE,
)
SHORT_TIME_RE = re.compile(
    "|".join([
        r"\bчерез\s+(?:\d+\s+)?(?:минут(?:у|ы)?|час|часа|часов)\b",
        r"\bin\s+(?:\d+|one|two|three|thirty)\s+(?:minute|minutes|hour|hours)\b",
        r"\btoday\b",
        r"\bсегодня\b",
        r"\bдо\s+\d{1,2}[:.]\d{2}\b",
        r"\bat\s+\d{1,2}[:.]\d{2}\b",
    ]),
    re.IGNORECASE,
)


def is_p0_candidate(text: str | None, caption: str | None = None) -> bool:
    combined = " ".join(part for part in [text, caption] if part)
    return bool(combined and P0_RE.search(combined))


def is_urgent_call_candidate(text: str | None, caption: str | None = None) -> bool:
    combined = " ".join(part for part in [text, caption] if part)
    return bool(
        combined
        and URGENT_CALL_RE.search(combined)
        and SHORT_TIME_RE.search(combined)
    )
