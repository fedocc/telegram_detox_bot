from __future__ import annotations

import re

_CHANNEL_WRAPPER_RE = re.compile(r"(?i)(?:^|\bэто\s+|[—–-]\s*)канал\s+с\b")
_CHANNEL_GENRE_ONLY_RE = re.compile(
    r"(?i)^\s*(?:философск\w*|юмористическ\w*|развлекательн\w*|"
    r"видеоконтент\w*|\w+\s+контент\w*)\s*[.!?]*\s*$"
)


def safe_truncate(text: str | None, limit: int = 500) -> str:
    if not text:
        return ""
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 3] + "..."


def sanitize_channel_summary(text: str | None) -> str:
    """Remove channel-genre wrappers without discarding post-level facts."""
    if not text:
        return ""
    compact = " ".join(text.split())
    cleaned_sentences: list[str] = []
    for sentence in re.split(r"(?<=[.!?])\s+", compact):
        candidate = sentence.strip()
        wrapper = _CHANNEL_WRAPPER_RE.search(candidate)
        if wrapper:
            _, separator, factual_clause = candidate[wrapper.end() :].partition(":")
            if separator and factual_clause.strip():
                cleaned_sentences.append(factual_clause.strip())
            continue
        if _CHANNEL_GENRE_ONLY_RE.fullmatch(candidate):
            continue
        cleaned_sentences.append(candidate)
    return " ".join(cleaned_sentences).strip()
