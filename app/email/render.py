from __future__ import annotations

from html import escape

from app.models.schemas import DailyDigest

INTERNAL_REVIEW_REASONS = {
    "LLM did not classify this incoming private message": "Требуется проверить сообщение",
    "Fallback digest includes incoming private message": "Личное сообщение",
    "P0 review candidate": "Возможно важное сообщение",
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


def _plain_deadline(item) -> str:
    deadline = _deadline_value(item)
    return f"; срок: {deadline}" if deadline else ""


def _html_deadline(item) -> str:
    deadline = _deadline_value(item)
    return f" Срок: {_line(deadline)}." if deadline else ""


def _time(value) -> str | None:
    return value.strftime("%H:%M") if value else None


def _metrics(item) -> str:
    parts = []
    if getattr(item, "message_count", None):
        parts.append(f"Сообщений: {item.message_count}.")
    first = _time(getattr(item, "first_message_at", None))
    last = _time(getattr(item, "last_message_at", None))
    if first:
        parts.append(f"Первое: {first}.")
    if last:
        parts.append(f"Последнее: {last}.")
    return " " + " ".join(parts) if parts else ""


def _semantic_details(item) -> str:
    parts = [
        f"Запросы: {getattr(item, 'requests_to_me', None) or 'Не определено.'}.",
        f"Контекст: {getattr(item, 'important_context', None) or 'Не определено.'}.",
        f"Действия: {getattr(item, 'action_items', None) or 'Не определено; проверьте чат.'}.",
    ]
    if getattr(item, "should_open_telegram", None) is True:
        reason = getattr(item, "open_reason", None) or "нужна проверка контекста"
        parts.append(f"Открыть Telegram: да — {reason}.")
    elif getattr(item, "should_open_telegram", None) is False:
        parts.append("Открыть Telegram: нет.")
    else:
        parts.append("Открыть Telegram: не определено.")
    if getattr(item, "media_summary", None):
        parts.append(item.media_summary)
    return " " + " ".join(parts)


def _review_reason(reason: str | None) -> str:
    return INTERNAL_REVIEW_REASONS.get(reason or "", reason or "Проверить")


def render_plain_text(digest: DailyDigest) -> str:
    parts = [f"Telegram digest — {digest.date}", "", "СРОЧНОЕ"]
    parts += [
        f"- {x.chat}: {x.summary} "
        f"({'уже отправлялось' if x.alert_sent else 'не отправлялось'})"
        f"{_plain_deadline(x)}"
        for x in digest.p0_alerts
    ] or ["- Нет"]
    parts += ["", "ЛИЧНЫЕ СООБЩЕНИЯ"]
    parts += [
        f"- {x.chat}: {_summary(x.what_happened or x.summary)}.{_semantic_details(x)}"
        f"{_metrics(x)}{_plain_deadline(x)}"
        for x in digest.direct_messages
    ] or ["- Нет"]
    parts += ["", "ГРУППЫ"]
    parts += [
        f"- {x.chat}: {_summary(x.what_happened or x.summary)}.{_semantic_details(x)}"
        f"{_metrics(x)}{_plain_deadline(x)}"
        for x in digest.group_updates
    ] or ["- Нет"]
    parts += ["", "ПРОВЕРИТЬ ЛИЧНО"]
    parts += [
        f"- {x.chat}: {_review_reason(x.reason)} — {x.summary}"
        for x in digest.review
    ] or ["- Нет"]
    parts += ["", "ФОН"]
    parts += [f"- {x.chat}: {x.count}" for x in digest.noise_counts] or ["- Нет"]
    return "\n".join(parts)


def render_html(digest: DailyDigest) -> str:
    def items(rows: list[str]) -> str:
        if not rows:
            return "<p>Нет</p>"
        return "<ul>" + "".join(f"<li>{row}</li>" for row in rows) + "</ul>"

    p0 = items([
        f"<b>{_line(x.chat)}</b>: {_line(x.summary)} "
        f"<i>{'Уведомление уже отправлялось.' if x.alert_sent else ''}</i>"
        f"{_html_deadline(x)}"
        for x in digest.p0_alerts
    ])
    direct = items([
        f"<b>{_line(x.chat)}</b>: {_line(_summary(x.what_happened or x.summary))}."
        f"{_line(_semantic_details(x))}{_line(_metrics(x))}{_html_deadline(x)}"
        for x in digest.direct_messages
    ])
    groups = items([
        f"<b>{_line(x.chat)}</b>: {_line(_summary(x.what_happened or x.summary))}."
        f"{_line(_semantic_details(x))}{_line(_metrics(x))}{_html_deadline(x)}"
        for x in digest.group_updates
    ])
    review = items([
        f"<b>{_line(x.chat)}</b>: {_line(_review_reason(x.reason))} — {_line(x.summary)}"
        for x in digest.review
    ])
    noise = items([f"<b>{_line(x.chat)}</b>: {x.count} сообщений" for x in digest.noise_counts])
    return f"""<!doctype html>
<html><body>
<h1>Telegram digest — {_line(digest.date)}</h1>
<h2>СРОЧНОЕ</h2>{p0}
<h2>ЛИЧНЫЕ СООБЩЕНИЯ</h2>{direct}
<h2>ГРУППЫ</h2>{groups}
<h2>ПРОВЕРИТЬ ЛИЧНО</h2>{review}
<h2>ФОН</h2>{noise}
</body></html>"""


def digest_subject(digest: DailyDigest) -> str:
    actions = sum(1 for x in [*digest.direct_messages, *digest.group_updates] if x.action)
    urgent = len(digest.p0_alerts)
    return f"Telegram digest — {digest.date} — {actions} действия, {urgent} срочное"
