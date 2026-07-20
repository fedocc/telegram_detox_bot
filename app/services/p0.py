from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from app.config import Settings
from app.db import repository
from app.email.sender import EmailSender
from app.ignored_chats import load_ignored_chats_from_settings
from app.llm.client import HaikuClient, LLMError
from app.models.schemas import (
    P0_MIN_CONFIDENCE,
    ChatType,
    MediaType,
    P0Decision,
    P0Status,
    StoredMessage,
)
from app.services.text import safe_truncate
from app.services.time_format import (
    format_user_datetime,
    format_user_time,
    localize_embedded_utc_iso,
)

SAFE_TEXT_LIMIT = 500
DEFAULT_MAX_CONTEXT_MESSAGES = 5
DEFAULT_MAX_MESSAGE_CHARS = 1000
DEFAULT_MAX_LLM_CALLS_PER_HOUR = 100
EMAIL_CONTEXT_LIMIT = 10
EMAIL_CONTEXT_WINDOW = timedelta(minutes=60)
PolicyContext = dict[str, bool | str | None]

REQUEST_OR_ACTION_RE = re.compile(
    r"(?<!\w)(?:"
    r"ответь|отпиши|напиши|скажи|позвони|набери|посмотри|проверь|пришли|"
    r"отправь|подтверди|посмотрите|проверьте|пришлите|отправьте|подтвердите|"
    r"можешь\s+(?:(?:сегодня|завтра|сейчас|потом)\s+)?"
    r"(?:ответить|написать|позвонить|посмотреть|проверить|прислать|отправить|"
    r"подтвердить|созвониться|встретиться)|"
    r"нужен\s+(?:ответ|код|файл)|"
    r"(?:надо|нужно)\s+(?:обсудить|поговорить|решить|проверить|ответить)|"
    r"(?:можно\s+вопрос|есть\s+минутка)|"
    r"дай\s+знать|созвонимся|"
    r"кто\s+может\s+(?:ответить|проверить|посмотреть)|"
    r"call\s+me|reply\s+please|"
    r"can\s+we\s+call|"
    r"(?:can|could)\s+you\s+(?:reply|call|check|send|confirm)|"
    r"send\s+me|check\s+this|need\s+(?:an?\s+)?(?:answer|reply|file|code)"
    r")(?!\w)",
    re.IGNORECASE,
)
PLANNING_OR_AVAILABILITY_RE = re.compile(
    r"(?<!\w)(?:"
    r"(?:сегодня|завтра)\s+сможешь|"
    r"сможешь\s+(?:сегодня|завтра)|"
    r"можешь\s+(?:сегодня|завтра)|"
    r"(?:сегодня|завтра)\s+свобод(?:ен|на)|"
    r"будешь\s+свобод(?:ен|на)|"
    r"получится\s+(?:(?:сегодня|завтра)\s+)?(?:встретиться|созвониться)|"
    r"(?:ид[её]м|пойд[её]м|го|давай)"
    r"(?=[^.!?\n]{0,60}(?:\?|(?:сегодня|завтра)\b))|"
    r"(?:сегодня|завтра)\b[^.!?\n]{0,40}\b"
    r"(?:ид[её]м|пойд[её]м|увидимся|встретимся|погулять|встретиться|"
    r"созвониться|свободен|свободна)|"
    r"(?:увидимся|встретимся|погулять|встретиться|созвониться)"
    r"(?=[^.!?\n]{0,40}\?)|"
    r"(?:го|давай)\b[^.!?\n]{0,40}\b"
    r"(?:гулять|погулять|увидеться|встретиться|созвониться)|"
    r"are\s+you\s+(?:free|available)|can\s+you\s+(?:meet|talk)"
    r")(?!\w)",
    re.IGNORECASE,
)
PRIVATE_DIRECT_REQUEST_RE = re.compile(
    r"(?<!\w)(?:"
    r"ответь|ответить|ответишь|напиши|скажи|позвони|посмотри|проверь|скинь|отправь"
    r")(?!\w)",
    re.IGNORECASE,
)
PRIVATE_PLANNING_TIME_RE = re.compile(
    r"(?<!\w)(?:"
    r"сегодня|завтра|сейчас|вечером|утром|"
    r"в\s+(?:[01]?\d|2[0-3])(?::[0-5]\d)?"
    r")(?!\w)",
    re.IGNORECASE,
)
PRIVATE_PLANNING_ACTION_RE = re.compile(
    r"(?<!\w)(?:"
    r"пойд[её]шь|прид[её]шь|сможешь|будешь|ид[её]м|го|гулять|встретиться|встреча"
    r")(?!\w)",
    re.IGNORECASE,
)
PRIVATE_TIME_SENSITIVE_RE = re.compile(
    r"(?<!\w)(?:самол[её]т|поезд|аэропорт|вылет|дедлайн|встреча)(?!\w)|"
    r"(?<!\w)завтра\s+в\s+(?:[01]?\d|2[0-3])(?::[0-5]\d)?(?!\w)",
    re.IGNORECASE,
)
PRIVATE_PING_RE = re.compile(r"(?<!\w)ал{1,2}о(?!\w)", re.IGNORECASE)
PRIVATE_PING_REPLY_RE = re.compile(
    r"(?<!\w)(?:ответь|ответишь|можешь\s+ответить)(?!\w)",
    re.IGNORECASE,
)
URGENCY_RE = re.compile(r"\b(?:asap|important|urgent|важн\w*|сроч\w*)\b", re.IGNORECASE)
IMPORTANT_CONTEXT_RE = re.compile(
    r"\b(?:авари\w*|блокиров\w*|доступ\w*|интервью|ошибк\w*|плат[её]ж\w*|"
    r"проблем\w*|собес\w*|суд\w*|не\s+работает|сломал\w*)\b",
    re.IGNORECASE,
)
EXPLICIT_DEADLINE_RE = re.compile(
    r"\b(?:at\s+\d{1,2}(?::\d{2})?|by\s+\d{1,2}(?::\d{2})?|deadline|дедлайн|"
    r"in\s+(?:\d+|one|two|three|thirty)\s+(?:minutes?|hours?)|"
    r"within\s+\d+\s+(?:minutes?|hours?)|до\s+\d{1,2}(?::\d{2})?|"
    r"до\s+завтра|by\s+tomorrow|"
    r"сегодня\s+до\s+\d{1,2}(?::\d{2})?|срок\s+(?:сегодня|завтра)|"
    r"через\s+(?:\d+\s+)?(?:минут\w*|час\w*))\b",
    re.IGNORECASE,
)
SMALL_TALK_PHRASES = (
    "hi how are you",
    "how are you",
    "what s up",
    "привет как дела",
    "как дела у тебя",
    "как дела",
    "что делаешь",
    "как ты",
    "обычная болтовня",
    "пишу просто так",
)
ISO_TIMESTAMP_RE = re.compile(
    r"\b\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?"
    r"(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?\b",
    re.IGNORECASE,
)


def _message_payload(
    message: StoredMessage,
    context: list,
    max_message_chars: int,
    *,
    trusted_sender: bool,
    policy_context: PolicyContext,
) -> dict:
    capped_text = safe_truncate(message.text or message.caption, max_message_chars)
    return {
        "message": {
            "chat_id": message.chat_id,
            "chat_title": message.chat_title,
            "chat_type": message.chat_type.value,
            "sender_name": message.sender_name,
            "message_id": message.message_id,
            "timestamp": message.timestamp.isoformat(),
            "text": capped_text,
            "media_type": message.media_type.value,
            "is_outgoing": message.is_outgoing,
            "trusted_sender": trusted_sender,
            "policy": policy_context,
        },
        "context": [
            {
                "sender": row.sender_name,
                "is_outgoing": row.is_outgoing,
                "text": safe_truncate(row.text or row.caption, SAFE_TEXT_LIMIT),
                "message_id": row.message_id,
            }
            for row in context
        ],
    }


def _context_with_reply_parent(session: Session, message: StoredMessage, limit: int) -> list:
    if limit <= 0:
        context = []
    else:
        context = repository.recent_chat_context(session, message.chat_id, limit=limit)
    if message.reply_to_message_id:
        parent = repository.get_message(session, message.chat_id, message.reply_to_message_id)
        if parent and all(row.message_id != parent.message_id for row in context):
            context.insert(0, parent)
    return context[-limit:] if limit > 0 else context[:1]


def _normalized_words(raw_text: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", raw_text.casefold()).split())


def _is_obvious_small_talk(raw_text: str) -> bool:
    normalized = _normalized_words(raw_text)
    return any(phrase in normalized for phrase in SMALL_TALK_PHRASES)


def _deadline_label(message: StoredMessage) -> str:
    raw_text = message.text or message.caption or ""
    lowered = raw_text.casefold()
    iso_timestamp = ISO_TIMESTAMP_RE.search(raw_text)
    if iso_timestamp:
        return localize_embedded_utc_iso(iso_timestamp.group(0))
    exact = re.search(r"\b(до|к)\s+(\d{1,2}(?::\d{2})?)\b", lowered)
    if exact:
        return f"{exact.group(1)} {exact.group(2)}"
    english_time = re.search(r"\b(by|at)\s+(\d{1,2}(?::\d{2})?)\b", lowered)
    if english_time:
        prefix = "до" if english_time.group(1) == "by" else "к"
        return f"{prefix} {english_time.group(2)}"
    if re.search(r"\b(?:до\s+завтра|by\s+tomorrow)\b", lowered):
        return "до завтра"
    if re.search(r"\b(?:сейчас|now)\b", lowered):
        return "сейчас"
    if re.search(r"\b(?:сегодня|today)\b", lowered):
        return "сегодня"
    if re.search(r"\b(?:завтра|tomorrow)\b", lowered):
        return "завтра"
    return "не указан"


def _as_utc_naive(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def _email_context(session: Session, message: StoredMessage) -> list:
    rows = repository.recent_chat_context(
        session,
        message.chat_id,
        limit=EMAIL_CONTEXT_LIMIT + 1,
    )
    current_at = _as_utc_naive(message.timestamp)
    prior = [
        row
        for row in rows
        if (row.chat_id, row.message_id) != (message.chat_id, message.message_id)
        and _as_utc_naive(row.timestamp) <= current_at
    ]
    recent = [
        row
        for row in prior
        if _as_utc_naive(row.timestamp) >= current_at - EMAIL_CONTEXT_WINDOW
    ]
    return (recent or prior[-5:])[-EMAIL_CONTEXT_LIMIT:]


def _context_line(row) -> str:
    sender = "Я" if row.is_outgoing else (row.sender_name or "Неизвестный отправитель")
    raw_text = safe_truncate(
        localize_embedded_utc_iso(
            row.text or row.caption or "[медиа без подписи]"
        ),
        DEFAULT_MAX_MESSAGE_CHARS,
    )
    return f"- {format_user_time(row.timestamp)} — {sender}: {raw_text}"


def _russian_reason(
    message: StoredMessage,
    policy_context: PolicyContext,
) -> str:
    if policy_context["direct_mention"]:
        mention_username = policy_context.get("direct_mention_username")
        if isinstance(mention_username, str):
            return f"Сообщение содержит прямое упоминание @{mention_username}."
        return "Сообщение содержит прямое упоминание."
    if _is_private(message):
        if policy_context["request_or_urgency"] or policy_context["response_expected"]:
            return "похоже, от тебя ждут ответа или действия."
        return "в личном сообщении есть важная информация, на которую стоит отреагировать."
    if policy_context["reply_to_me"]:
        return "это ответ на твоё сообщение."
    if policy_context["urgent_or_important"]:
        return "в групповом сообщении есть явная срочность или важность."
    if policy_context["explicit_deadline"]:
        return "в групповом сообщении указан срок, который может требовать реакции."
    if policy_context["watchlist_match"]:
        return "сообщение совпало с настроенным ключевым словом или важным чатом."
    return "похоже, от тебя или участников ждут реакции."


def _russian_action(
    message: StoredMessage,
    policy_context: PolicyContext | None = None,
) -> str:
    if policy_context and policy_context["direct_mention"]:
        return "Открыть Telegram и ответить."
    if _is_private(message):
        return "ответить в Telegram."
    return "проверить сообщение и при необходимости ответить в Telegram."


def _decision_body(
    session: Session,
    message: StoredMessage,
    policy_context: PolicyContext,
) -> str:
    raw_text = localize_embedded_utc_iso(message.text or message.caption)
    context = _email_context(session, message)
    rendered_context = (
        "\n".join(_context_line(row) for row in context)
        if context
        else "Предыдущих сообщений нет."
    )
    parts = [
        f"Чат: {message.chat_title}",
        f"Отправитель: {message.sender_name or 'Неизвестный отправитель'}",
        f"Время: {format_user_datetime(message.timestamp)}",
        f"Почему срочно: {_russian_reason(message, policy_context)}",
        f"Что сделать: {_russian_action(message, policy_context)}",
        f"Срок: {_deadline_label(message)}",
        f"Исходный текст:\n{raw_text}",
        f"Контекст переписки:\n{rendered_context}",
    ]
    return localize_embedded_utc_iso("\n\n".join(parts))


def _has_text(message: StoredMessage) -> bool:
    return bool((message.text or message.caption or "").strip())


def _is_private(message: StoredMessage) -> bool:
    return message.chat_type == ChatType.private


def _is_channel(message: StoredMessage) -> bool:
    return message.chat_type == ChatType.channel


def _is_groupish(message: StoredMessage) -> bool:
    return message.chat_type in {ChatType.group, ChatType.supergroup, ChatType.channel}


def _is_non_text_media(message: StoredMessage) -> bool:
    return message.media_type != MediaType.none and not _has_text(message)


def _mention_usernames(settings: Settings | None) -> set[str]:
    configured = (
        settings.p0_mention_usernames
        if settings is not None
        else "fedocc,me,fedornikonov"
    )
    return {
        item.strip().removeprefix("@").casefold()
        for item in configured.split(",")
        if item.strip().removeprefix("@")
    }


def _mention_usernames_from_config(configured: str) -> set[str]:
    return {
        item.strip().removeprefix("@").casefold()
        for item in configured.split(",")
        if item.strip().removeprefix("@")
    }


def matched_mention_username_from_config(text: str, configured: str) -> str | None:
    usernames = _mention_usernames_from_config(configured)
    for match in re.finditer(r"(?<!\w)@([A-Za-z0-9_]+)(?![A-Za-z0-9_])", text):
        username = match.group(1).casefold()
        if username in usernames:
            return username
    return None


def matched_mention_username(
    text: str,
    settings: Settings | None,
) -> str | None:
    configured = _mention_usernames(settings)
    for match in re.finditer(r"(?<!\w)@([A-Za-z0-9_]+)(?![A-Za-z0-9_])", text):
        username = match.group(1).casefold()
        if username in configured:
            return username
    return None


def _matched_mention_username(
    message: StoredMessage,
    settings: Settings | None,
) -> str | None:
    return matched_mention_username(message.text or message.caption or "", settings)


def _replies_to_me(session: Session, message: StoredMessage) -> bool:
    if not message.reply_to_message_id:
        return False
    if message.reply_to_is_mine is not None:
        return message.reply_to_is_mine
    parent = repository.get_message(session, message.chat_id, message.reply_to_message_id)
    if parent is None:
        # TODO: Persist reply-parent direction from Telegram when the parent is unavailable.
        return False
    return parent.is_outgoing


def _watchlist_contains(settings: Settings | None, chat_id: str) -> bool:
    if settings is None:
        return False
    watched = {
        item.strip()
        for item in settings.p0_watchlist_chat_ids.split(",")
        if item.strip()
    }
    return chat_id in watched


def _trusted_sender(settings: Settings | None, message: StoredMessage) -> bool:
    if settings is None or not message.sender_id:
        return False
    trusted = {
        item.strip()
        for item in settings.p0_trusted_sender_ids.split(",")
        if item.strip()
    }
    return message.sender_id in trusted


def _watchlist_keyword_matches(settings: Settings | None, raw_text: str) -> bool:
    if settings is None:
        return False
    normalized = raw_text.casefold()
    return any(
        keyword.strip().casefold() in normalized
        for keyword in settings.p0_watchlist_keywords.split(",")
        if keyword.strip()
    )


def _has_request_or_action(raw_text: str) -> bool:
    return bool(REQUEST_OR_ACTION_RE.search(raw_text))


def _has_planning_or_availability(raw_text: str) -> bool:
    return bool(PLANNING_OR_AVAILABILITY_RE.search(raw_text))


def _has_urgency(raw_text: str) -> bool:
    return bool(URGENCY_RE.search(raw_text))


def _has_important_context(raw_text: str) -> bool:
    return bool(IMPORTANT_CONTEXT_RE.search(raw_text))


def _response_may_be_expected(raw_text: str) -> bool:
    has_meaningful_question = bool(
        "?" in raw_text and any(len(token) >= 3 for token in re.findall(r"\w+", raw_text))
    )
    return bool(
        has_meaningful_question
        or _has_request_or_action(raw_text)
        or _has_planning_or_availability(raw_text)
    )


def _private_response_may_be_expected(raw_text: str) -> bool:
    return bool(
        _has_request_or_action(raw_text) or _has_planning_or_availability(raw_text)
    )


def _private_deterministic_signal(raw_text: str) -> str | None:
    if PRIVATE_PING_RE.search(raw_text) and PRIVATE_PING_REPLY_RE.search(raw_text):
        return "private_ping_reply"
    if PRIVATE_DIRECT_REQUEST_RE.search(raw_text):
        return "private_direct_request"
    if PRIVATE_TIME_SENSITIVE_RE.search(raw_text):
        return "private_time_sensitive"
    if PRIVATE_PLANNING_TIME_RE.search(raw_text) and PRIVATE_PLANNING_ACTION_RE.search(
        raw_text
    ):
        return "private_planning"
    return None


def _has_private_signal(raw_text: str) -> bool:
    return bool(
        _private_deterministic_signal(raw_text)
        or _has_request_or_action(raw_text)
        or _has_planning_or_availability(raw_text)
        or _has_urgency(raw_text)
        or _has_important_context(raw_text)
        or EXPLICIT_DEADLINE_RE.search(raw_text)
    )


def is_deterministic_p0_equivalent(
    chat_type: ChatType,
    text: str,
    *,
    mention_usernames: str = "fedocc,me,fedornikonov",
    reply_to_me: bool = False,
) -> bool:
    mentioned = matched_mention_username_from_config(text, mention_usernames) is not None
    if chat_type == ChatType.channel:
        return mentioned
    if chat_type == ChatType.private:
        return bool(mentioned or _has_private_signal(text))
    return bool(
        mentioned
        or reply_to_me
        or _has_request_or_action(text)
        or _has_urgency(text)
        or _has_important_context(text)
        or EXPLICIT_DEADLINE_RE.search(text)
        or _response_may_be_expected(text)
    )


def _group_policy_context(
    session: Session,
    message: StoredMessage,
    settings: Settings | None,
) -> PolicyContext:
    raw_text = message.text or message.caption or ""
    direct_mention_username = _matched_mention_username(message, settings)
    direct_mention = direct_mention_username is not None
    reply_to_me = False if _is_channel(message) else _replies_to_me(session, message)
    request_or_action = _has_request_or_action(raw_text)
    urgent_or_important = _has_urgency(raw_text) or _has_important_context(raw_text)
    response_expected = _response_may_be_expected(raw_text)
    request_or_urgency = request_or_action or urgent_or_important
    explicit_deadline = bool(EXPLICIT_DEADLINE_RE.search(raw_text))
    watchlist_match = bool(
        _watchlist_keyword_matches(settings, raw_text)
        or (
            settings is not None
            and settings.p0_classify_watchlist_chats
            and _watchlist_contains(settings, message.chat_id)
        )
    )
    mention_enabled = settings is None or settings.p0_classify_mentions
    reply_enabled = settings is None or settings.p0_classify_replies
    watchlist_enabled = settings is None or settings.p0_classify_watchlist_chats
    if _is_channel(message):
        deterministic_strict = bool(mention_enabled and direct_mention)
    else:
        deterministic_strict = bool(
            (mention_enabled and direct_mention)
            or (reply_enabled and reply_to_me)
            or request_or_action
            or urgent_or_important
            or explicit_deadline
            or response_expected
            or (watchlist_enabled and watchlist_match)
        )
    return {
        "small_talk": False,
        "direct_mention": direct_mention,
        "direct_mention_username": direct_mention_username,
        "reply_to_me": reply_to_me,
        "request_or_urgency": request_or_urgency,
        "response_expected": response_expected,
        "urgent_or_important": urgent_or_important,
        "explicit_deadline": explicit_deadline,
        "watchlist_match": watchlist_match,
        "deterministic_strict": deterministic_strict,
    }


def _policy_context(
    session: Session,
    message: StoredMessage,
    settings: Settings | None,
) -> PolicyContext:
    raw_text = message.text or message.caption or ""
    if _is_private(message):
        direct_mention_username = _matched_mention_username(message, settings)
        direct_mention = direct_mention_username is not None
        mention_enabled = settings is None or settings.p0_classify_mentions
        private_deterministic_signal = _private_deterministic_signal(raw_text)
        request_or_action = bool(
            _has_request_or_action(raw_text)
            or private_deterministic_signal
            in {"private_direct_request", "private_ping_reply"}
        )
        planning_or_availability = bool(
            _has_planning_or_availability(raw_text)
            or private_deterministic_signal == "private_planning"
        )
        urgent_or_important = bool(
            _has_urgency(raw_text)
            or _has_important_context(raw_text)
            or private_deterministic_signal == "private_time_sensitive"
        )
        response_expected = bool(
            _private_response_may_be_expected(raw_text)
            or request_or_action
            or planning_or_availability
        )
        request_or_urgency = (
            request_or_action or planning_or_availability or urgent_or_important
        )
        private_signal = bool(
            _has_private_signal(raw_text) or (mention_enabled and direct_mention)
        )
        small_talk = _is_obvious_small_talk(raw_text) and not private_signal
        deterministic_strict = private_signal
        return {
            "small_talk": small_talk,
            "private_signal": private_signal,
            "private_deterministic_signal": private_deterministic_signal,
            "direct_mention": direct_mention,
            "direct_mention_username": direct_mention_username,
            "reply_to_me": False,
            "request_or_urgency": request_or_urgency,
            "response_expected": response_expected,
            "urgent_or_important": urgent_or_important,
            "explicit_deadline": bool(EXPLICIT_DEADLINE_RE.search(raw_text)),
            "watchlist_match": False,
            "deterministic_strict": deterministic_strict,
        }
    return {"private_signal": False, **_group_policy_context(session, message, settings)}


def _should_classify_immediately(
    session: Session,
    message: StoredMessage,
    settings: Settings | None,
    policy_context: PolicyContext,
) -> bool:
    if message.is_outgoing or not _has_text(message):
        return False
    if _is_private(message):
        return True if settings is None else settings.p0_classify_private_text
    if not _is_groupish(message):
        return False
    if _is_channel(message):
        return policy_context["deterministic_strict"]
    if settings is not None and settings.p0_classify_all_groups:
        return True
    return policy_context["deterministic_strict"]


def debug_p0_check(
    chat_type: ChatType,
    text: str,
    settings: Settings,
    *,
    is_outgoing: bool = False,
) -> dict[str, bool | str]:
    if is_outgoing:
        return {
            "is_p0": False,
            "reason_category": "self_message",
            "matched_signal": "self_message",
            "chat_type": chat_type.value,
            "is_outgoing": True,
        }
    mention = matched_mention_username(text, settings)
    if settings.p0_classify_mentions and mention is not None:
        return {
            "is_p0": True,
            "reason_category": "direct_mention",
            "matched_signal": "mention",
            "chat_type": chat_type.value,
            "is_outgoing": False,
        }
    if chat_type == ChatType.channel:
        return {
            "is_p0": False,
            "reason_category": "channel_digest_only",
            "matched_signal": "none",
            "chat_type": chat_type.value,
            "is_outgoing": False,
        }
    if chat_type == ChatType.private:
        private_deterministic_signal = _private_deterministic_signal(text)
        matched = _has_private_signal(text)
        reason_category = "private_signal" if matched else "none"
        matched_signal = private_deterministic_signal or ("policy" if matched else "none")
    else:
        matched = bool(
            _has_request_or_action(text)
            or _has_urgency(text)
            or _has_important_context(text)
            or EXPLICIT_DEADLINE_RE.search(text)
            or _response_may_be_expected(text)
        )
        reason_category = "group_signal" if matched else "none"
        matched_signal = "policy" if matched else "none"
    return {
        "is_p0": matched,
        "reason_category": reason_category,
        "matched_signal": matched_signal,
        "chat_type": chat_type.value,
        "is_outgoing": False,
    }


def _max_context_messages(settings: Settings | None) -> int:
    if settings is None:
        return DEFAULT_MAX_CONTEXT_MESSAGES
    return settings.p0_max_context_messages


def _max_message_chars(settings: Settings | None) -> int:
    if settings is None:
        return DEFAULT_MAX_MESSAGE_CHARS
    return settings.p0_max_message_chars


def _hourly_cap(settings: Settings | None) -> int:
    if settings is None:
        return DEFAULT_MAX_LLM_CALLS_PER_HOUR
    return settings.p0_max_llm_calls_per_hour


def _send_immediate_alert(
    session: Session,
    message: StoredMessage,
    email_sender: EmailSender,
    *,
    subject: str,
    body: str,
    html: str | None,
    alert_type: str,
) -> bool:
    job = repository.create_alert_job(
        session,
        chat_id=message.chat_id,
        message_id=message.message_id,
        alert_type=alert_type,
        subject=subject,
        text_body=body,
        html_body=html or "",
        now=message.timestamp,
    )
    if job.status != "pending" or job.attempts > 0:
        return True
    if repository.send_alert_job(session, job, email_sender, message.timestamp):
        repository.mark_alert_sent(session, message.chat_id, message.message_id)
    return True


def _mark_budget_review(
    session: Session,
    message: StoredMessage,
) -> bool:
    repository.mark_p0_review_candidate(session, message.chat_id, message.message_id)
    repository.mark_p0_classified(
        session,
        message.chat_id,
        message.message_id,
        P0Status.p0_candidate.value,
        message.timestamp,
    )
    return False


def _mark_candidate(
    session: Session,
    message: StoredMessage,
    confidence: float | None = None,
) -> bool:
    repository.mark_p0_review_candidate(session, message.chat_id, message.message_id)
    repository.mark_p0_classified(
        session,
        message.chat_id,
        message.message_id,
        P0Status.p0_candidate.value,
        message.timestamp,
        confidence=confidence,
    )
    return False


def _mark_not_p0(
    session: Session,
    message: StoredMessage,
    confidence: float | None = None,
) -> bool:
    repository.mark_p0_classified(
        session,
        message.chat_id,
        message.message_id,
        P0Status.not_p0.value,
        message.timestamp,
        confidence=confidence,
    )
    return False


def _local_strict_reason(
    message: StoredMessage,
    policy_context: PolicyContext,
) -> str:
    return _russian_reason(message, policy_context)


def _promote_to_strict(
    message: StoredMessage,
    policy_context: PolicyContext,
    decision: P0Decision | None = None,
) -> P0Decision:
    reason = _local_strict_reason(message, policy_context)
    return P0Decision(
        status=P0Status.p0_strict,
        summary=reason,
        reason=reason,
        action=_russian_action(message, policy_context),
        deadline_text=decision.deadline_text if decision else None,
        deadline_at=decision.deadline_at if decision else None,
        confidence=max(decision.confidence if decision else 1.0, P0_MIN_CONFIDENCE),
    )


def _decision_qualifies_for_strict(
    message: StoredMessage,
    policy_context: PolicyContext,
    decision: P0Decision,
) -> bool:
    return bool(
        policy_context["deterministic_strict"]
        or (
            _is_private(message)
            and policy_context["private_signal"]
            and decision.status == P0Status.p0_strict
            and decision.confidence >= P0_MIN_CONFIDENCE
        )
    )


def _send_strict_decision(
    session: Session,
    message: StoredMessage,
    decision: P0Decision,
    email_sender: EmailSender,
    policy_context: PolicyContext,
) -> bool:
    repository.mark_p0_classified(
        session,
        message.chat_id,
        message.message_id,
        P0Status.p0_strict.value,
        message.timestamp,
        confidence=decision.confidence,
    )
    return _send_immediate_alert(
        session,
        message,
        email_sender,
        subject="Telegram alert",
        body=_decision_body(session, message, policy_context),
        html=None,
        alert_type="p0",
    )


def handle_p0_candidate(
    session: Session,
    message: StoredMessage,
    llm: HaikuClient,
    email_sender: EmailSender,
    settings: Settings | None = None,
    ignored_chat_ids: frozenset[str] | set[str] | None = None,
) -> bool:
    if ignored_chat_ids is None and settings is not None:
        ignored_chat_ids = load_ignored_chats_from_settings(settings).chat_ids
    if ignored_chat_ids and message.chat_id in ignored_chat_ids:
        return False
    if message.is_outgoing:
        return _mark_not_p0(session, message)
    existing = repository.get_message(session, message.chat_id, message.message_id)
    if existing and existing.alert_sent:
        return False
    if existing and existing.p0_classified_at:
        return False

    if _is_non_text_media(message):
        return _mark_not_p0(session, message)

    policy_context = _policy_context(session, message, settings)
    if not _should_classify_immediately(session, message, settings, policy_context):
        return False

    cap = _hourly_cap(settings)
    since = message.timestamp - timedelta(hours=1)
    if cap <= repository.p0_llm_calls_since(session, since):
        if policy_context["deterministic_strict"]:
            decision = _promote_to_strict(message, policy_context)
            return _send_strict_decision(
                session,
                message,
                decision,
                email_sender,
                policy_context,
            )
        return _mark_budget_review(session, message)

    context = _context_with_reply_parent(session, message, _max_context_messages(settings))
    try:
        repository.mark_p0_llm_called(
            session,
            message.chat_id,
            message.message_id,
            message.timestamp,
        )
        decision = llm.classify_p0(
            _message_payload(
                message,
                context,
                _max_message_chars(settings),
                trusted_sender=_trusted_sender(settings, message),
                policy_context=policy_context,
            )
        )
        if _decision_qualifies_for_strict(message, policy_context, decision):
            decision = _promote_to_strict(message, policy_context, decision)
            return _send_strict_decision(
                session,
                message,
                decision,
                email_sender,
                policy_context,
            )
        if decision.status == P0Status.not_p0:
            return _mark_not_p0(session, message, decision.confidence)
        return _mark_candidate(session, message, decision.confidence)
    except LLMError:
        if policy_context["deterministic_strict"]:
            decision = _promote_to_strict(message, policy_context)
            return _send_strict_decision(
                session,
                message,
                decision,
                email_sender,
                policy_context,
            )
        return _mark_candidate(session, message)
