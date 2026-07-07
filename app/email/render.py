from __future__ import annotations

from html import escape

from app.models.schemas import DailyDigest


def _line(text: str) -> str:
    return escape(text or "")


def _deadline_value(item) -> str | None:
    deadline_at = getattr(item, "deadline_at", None)
    if deadline_at:
        return deadline_at.isoformat()
    return getattr(item, "deadline_text", None)


def _plain_deadline(item) -> str:
    deadline = _deadline_value(item)
    return f"; срок: {deadline}" if deadline else ""


def _html_deadline(item) -> str:
    deadline = _deadline_value(item)
    return f" Срок: {_line(deadline)}." if deadline else ""


def render_plain_text(digest: DailyDigest) -> str:
    parts = [f"Telegram digest — {digest.date}", "", "СРОЧНОЕ ЗА ДЕНЬ"]
    parts += [
        f"- {x.chat}: {x.summary} "
        f"({'уже отправлялось' if x.alert_sent else 'не отправлялось'})"
        f"{_plain_deadline(x)}"
        for x in digest.p0_alerts
    ] or ["- Нет"]
    parts += ["", "ЛИЧНЫЕ СООБЩЕНИЯ"]
    parts += [
        f"- {x.chat}: {x.summary}; ответ: {'да' if x.needs_reply else 'нет'}"
        f"{_plain_deadline(x)}"
        for x in digest.direct_messages
    ] or ["- Нет"]
    parts += ["", "ГРУППЫ / ЛАБОРАТОРИЯ"]
    parts += [
        f"- {x.chat}: {x.summary}{_plain_deadline(x)}"
        for x in digest.group_updates
    ] or ["- Нет"]
    parts += ["", "ПРОВЕРИТЬ ЛИЧНО"]
    parts += [f"- {x.chat}: {x.reason} — {x.summary}" for x in digest.review] or ["- Нет"]
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
        f"<b>{_line(x.chat)}</b>: {_line(x.summary)} "
        f"Ответ нужен: {'да' if x.needs_reply else 'нет'}."
        f"{_html_deadline(x)}"
        for x in digest.direct_messages
    ])
    groups = items([
        f"<b>{_line(x.chat)}</b>: {_line(x.summary)}{_html_deadline(x)}"
        for x in digest.group_updates
    ])
    review = items([
        f"<b>{_line(x.chat)}</b>: {_line(x.reason)} — {_line(x.summary)}"
        for x in digest.review
    ])
    noise = items([f"<b>{_line(x.chat)}</b>: {x.count} сообщений" for x in digest.noise_counts])
    return f"""<!doctype html>
<html><body>
<h1>Telegram digest — {_line(digest.date)}</h1>
<h2>СРОЧНОЕ ЗА ДЕНЬ</h2>{p0}
<h2>ЛИЧНЫЕ СООБЩЕНИЯ</h2>{direct}
<h2>ГРУППЫ / ЛАБОРАТОРИЯ</h2>{groups}
<h2>ПРОВЕРИТЬ ЛИЧНО</h2>{review}
<h2>ФОН</h2>{noise}
</body></html>"""


def digest_subject(digest: DailyDigest) -> str:
    actions = sum(1 for x in [*digest.direct_messages, *digest.group_updates] if x.action)
    urgent = len(digest.p0_alerts)
    return f"Telegram digest — {digest.date} — {actions} действия, {urgent} срочное"
