"""Displayable Telegram content which isn't a downloadable photo/document."""

from __future__ import annotations

import math
from base64 import urlsafe_b64encode

from telethon.tl import types


def plain_text(value) -> str:
    # Telegram's newer layers use TextWithEntities; older polls used strings.
    return value if isinstance(value, str) else getattr(value, "text", "") or ""


def poll_option_id(value: bytes) -> str:
    return urlsafe_b64encode(value).decode("ascii").rstrip("=")


def poll_content(media) -> dict:
    poll, results = media.poll, media.results
    counts = {item.option: item for item in (getattr(results, "results", None) or ())}
    voted = any(getattr(item, "chosen", False) for item in counts.values())
    closed = bool(getattr(poll, "closed", False))
    visible = closed or voted
    if getattr(poll, "hide_results_until_close", False) and not closed:
        visible = False
    total = getattr(results, "total_voters", None)
    options = []
    for answer in poll.answers:
        count = counts.get(answer.option)
        voters = getattr(count, "voters", None) if visible else None
        options.append({
            "id": poll_option_id(answer.option),
            "text": plain_text(answer.text) or "Вариант с вложением",
            "voters": voters,
            "percent": round(100 * voters / total) if voters is not None and total else None,
            "chosen": bool(getattr(count, "chosen", False)),
            "correct": bool(visible and getattr(count, "correct", False)),
        })
    return {
        "kind": "poll", "id": str(poll.id),
        "title": plain_text(poll.question) or "Опрос",
        "quiz": bool(getattr(poll, "quiz", False)),
        "anonymous": not bool(getattr(poll, "public_voters", False)),
        "multiple": bool(getattr(poll, "multiple_choice", False)),
        "closed": closed, "total_voters": total, "options": options, "voted": voted,
        "can_revote": not bool(getattr(poll, "quiz", False)
                               or getattr(poll, "revoting_disabled", False)),
        "solution": (getattr(results, "solution", None) or "") if visible else "",
    }


def message_content(message) -> dict | None:
    media = getattr(message, "media", None)
    if media is None or isinstance(media, types.MessageMediaEmpty):
        return None
    if isinstance(media, types.MessageMediaPoll):
        return poll_content(media)
    if isinstance(media, types.MessageMediaContact):
        return {"kind": "contact", "label": "Контакт",
                "title": " ".join(filter(None, (media.first_name, media.last_name))) or "Контакт",
                "text": media.phone_number}
    if isinstance(media, (types.MessageMediaGeo, types.MessageMediaGeoLive,
                          types.MessageMediaVenue)):
        live = isinstance(media, types.MessageMediaGeoLive)
        result = {"kind": "location", "label": "Геопозиция в реальном времени" if live
                  else "Геопозиция", "title": getattr(media, "title", "") or "Геопозиция",
                  "text": getattr(media, "address", "") or ""}
        geo = media.geo
        lat, lon = getattr(geo, "lat", None), getattr(geo, "long", None)
        if (isinstance(lat, (float, int)) and isinstance(lon, (float, int))
                and math.isfinite(lat) and math.isfinite(lon)
                and -90 <= lat <= 90 and -180 <= lon <= 180):
            result.update(latitude=lat, longitude=lon)
        else:
            result["text"] = result["text"] or "Координаты недоступны"
        return result
    if isinstance(media, types.MessageMediaWebPage):
        page = media.webpage
        title = getattr(page, "title", None) or getattr(page, "site_name", None)
        description = getattr(page, "description", None) or ""
        url = getattr(page, "url", None)
        if title or description or url:
            return {"kind": "webpage", "label": "Ссылка", "title": title or url,
                    "text": description, "url": url}
        # Empty/pending previews add nothing to the accompanying URL text.
        if getattr(message, "raw_text", None):
            return None
    if isinstance(media, types.MessageMediaDice):
        return {"kind": "dice", "label": "Игральный кубик", "title": media.emoticon,
                "text": f"Результат: {media.value}" if media.value else "Бросок…"}
    if isinstance(media, types.MessageMediaGame):
        return {"kind": "game", "label": "Игра", "title": media.game.title,
                "text": media.game.description}
    if isinstance(media, types.MessageMediaInvoice):
        return {"kind": "invoice", "label": "Счёт", "title": media.title,
                "text": media.description}
    # Ordinary files keep the established download/streaming path.
    if getattr(message, "file", None):
        return None
    labels = {
        "MessageMediaPhoto": "Фото недоступно",
        "MessageMediaDocument": "Файл недоступен",
        "MessageMediaPaidMedia": "Платное вложение",
        "MessageMediaStory": "История",
        "MessageMediaGiveaway": "Розыгрыш",
        "MessageMediaGiveawayResults": "Результаты розыгрыша",
        "MessageMediaToDo": "Список задач",
    }
    return {"kind": "unsupported", "label": "Вложение",
            "title": labels.get(type(media).__name__, "Вложение"),
            "text": "Этот формат пока не поддерживается на сайте."}


def content_preview(content) -> str:
    if content["kind"] == "poll":
        return f"{'Викторина' if content['quiz'] else 'Опрос'}: {content['title']}"
    label, title = content.get("label", "Вложение"), content.get("title", "")
    return f"{label}: {title}" if title and title != label else label
