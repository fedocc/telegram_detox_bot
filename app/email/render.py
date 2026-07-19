from __future__ import annotations

from html import escape

from app.models.schemas import DailyDigest
from app.services.text import sanitize_channel_summary

EMPTY_VALUES = {
    "нет",
    "не определено",
    "не определено; проверьте чат",
    "явных запросов нет",
    "действий по переписке не указано",
    "дополнительный контекст не выделен",
    "полезные факты не выделены",
    "открыть telegram: нет",
}


def _line(text: str) -> str:
    return escape(text or "")


def _deadline_value(item) -> str | None:
    deadline_at = getattr(item, "deadline_at", None)
    if deadline_at:
        return deadline_at.isoformat()
    return getattr(item, "deadline_text", None)


def _summary(text: str) -> str:
    return (text or "").replace("короткая переписка", "обсуждение").strip()


def _time(value) -> str | None:
    return value.strftime("%H:%M") if value else None


def _optional(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = value.strip()
    normalized = " ".join(cleaned.casefold().split()).rstrip(" .!?:;").strip()
    if not cleaned or normalized in EMPTY_VALUES:
        return None
    return cleaned


def _metrics_line(item) -> str | None:
    parts: list[str] = []
    if getattr(item, "message_count", None):
        parts.append(f"Сообщений: {item.message_count}")
    media = _optional(getattr(item, "media_summary", None))
    if media:
        parts.append(f"медиа: {media}")
    return "; ".join(parts) or None


def _time_line(item) -> str | None:
    first = _time(getattr(item, "first_message_at", None))
    last = _time(getattr(item, "last_message_at", None))
    if first and last and first != last:
        return f"{first}–{last}"
    return last or first


def _important_line(item, *, channel: bool = False) -> str | None:
    values: list[str] = []
    for attribute in (
        "requests_to_me",
        "important_context",
        "action_items",
        "action",
    ):
        raw_value = getattr(item, attribute, None)
        if channel:
            raw_value = sanitize_channel_summary(raw_value)
        value = _optional(raw_value)
        if value and value not in values:
            values.append(value)
    return "; ".join(values) or None


def _limit_line(item) -> str | None:
    analyzed = getattr(item, "analyzed_message_count", None)
    total = getattr(item, "message_count", None)
    if analyzed is not None and total is not None and analyzed < total:
        return f"проанализировано {analyzed} из {total} сообщений."
    return None


def _item_lines(item, *, channel: bool = False) -> list[str]:
    lines = [item.chat]
    metrics = _metrics_line(item)
    if metrics:
        lines.append(f"- {metrics}")
    time_value = _time_line(item)
    if time_value:
        lines.append(f"- Время: {time_value}")
    summary_text = _summary(getattr(item, "what_happened", None) or item.summary)
    if channel:
        summary_text = sanitize_channel_summary(summary_text)
    summary = _optional(summary_text)
    if summary:
        lines.append(f"- Суть: {summary}")
    important = _important_line(item, channel=channel)
    if important:
        lines.append(f"- Важно: {important}")
    deadline = _optional(_deadline_value(item))
    if deadline:
        lines.append(f"- Срок: {deadline}")
    limit = _limit_line(item)
    if limit:
        lines.append(f"- Лимит: {limit}")
    return lines


def _plain_section(title: str, items: list, *, channel: bool = False) -> list[str]:
    lines = [title]
    if not items:
        return [*lines, "Нет"]
    for index, item in enumerate(items):
        if index:
            lines.append("")
        lines.extend(_item_lines(item, channel=channel))
    return lines


def render_plain_text(digest: DailyDigest) -> str:
    parts = ["Telegram digest", digest.date]
    for title, items in (
        ("СРОЧНОЕ", digest.p0_alerts),
        ("ЛИЧНЫЕ СООБЩЕНИЯ", digest.direct_messages),
        ("ГРУППЫ", digest.group_updates),
        ("КАНАЛЫ", digest.channel_updates),
    ):
        parts.extend(["", *_plain_section(title, items, channel=title == "КАНАЛЫ")])
    return "\n".join(parts)


def render_html(digest: DailyDigest) -> str:
    def section(title: str, rows: list, *, channel: bool = False) -> str:
        if not rows:
            body = "<p>Нет</p>"
        else:
            blocks = []
            for item in rows:
                lines = _item_lines(item, channel=channel)
                title_line, details = lines[0], lines[1:]
                detail_html = "".join(
                    f"<div style=\"margin-top:4px\">{_line(line.removeprefix('- '))}</div>"
                    for line in details
                )
                blocks.append(
                    "<div style=\"margin:0 0 18px 0\">"
                    f"<strong>{_line(title_line)}</strong>{detail_html}</div>"
                )
            body = "".join(blocks)
        return f"<h2 style=\"margin-top:28px\">{_line(title)}</h2>{body}"

    return f"""<!doctype html>
<html><body style="font-family:Arial,sans-serif;line-height:1.45;max-width:760px">
<h1>Telegram digest</h1>
<p>{_line(digest.date)}</p>
{section("СРОЧНОЕ", digest.p0_alerts)}
{section("ЛИЧНЫЕ СООБЩЕНИЯ", digest.direct_messages)}
{section("ГРУППЫ", digest.group_updates)}
{section("КАНАЛЫ", digest.channel_updates, channel=True)}
</body></html>"""


def digest_subject(digest: DailyDigest) -> str:
    return "Telegram digest"
