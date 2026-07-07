from __future__ import annotations

import logging
import re
import traceback
from collections.abc import Iterable

from app.config import Settings

SECRET_PATTERNS = [
    re.compile(r"(AITUNNEL_API_KEY|SMTP_PASSWORD|TG_API_HASH)=([^,\s]+)", re.I),
    re.compile(
        r"(api[_-]?key|password|api[_-]?hash|session)"
        r"(['\"]?\s*[:=]\s*['\"]?)([^'\"\s,]+)",
        re.I,
    ),
]


def _redact_value(message: str, value: object) -> str:
    if value is None:
        return message
    text = str(value)
    if not text:
        return message
    message = message.replace(text, "[REDACTED]")
    if text.endswith(".session"):
        message = message.replace(text.split("/")[-1], "[REDACTED_SESSION]")
    return message


class SecretRedactor(logging.Filter):
    def __init__(self, secret_values: Iterable[object] = ()):
        super().__init__()
        self.secret_values = tuple(secret_values)

    def redact(self, message: str) -> str:
        for value in self.secret_values:
            message = _redact_value(message, value)
        message = re.sub(r"[\w./-]+\.session(?:-journal)?", "[REDACTED_SESSION]", message)
        message = re.sub(
            r"session\s+contents?:\s*\S+",
            "session contents: [REDACTED]",
            message,
            flags=re.I,
        )
        for pattern in SECRET_PATTERNS:
            if pattern.groups >= 3:
                message = pattern.sub(r"\1\2[REDACTED]", message)
            else:
                message = pattern.sub(r"\1=[REDACTED]", message)
        return message

    def filter(self, record: logging.LogRecord) -> bool:
        message = self.redact(record.getMessage())
        record.msg = message
        record.args = ()
        if record.exc_info:
            record.exc_text = self.redact("".join(traceback.format_exception(*record.exc_info)))
            record.exc_info = None
        return True


def configure_logging(settings: Settings | None = None) -> None:
    secret_values: list[object] = []
    if settings is not None:
        secret_values = [
            settings.aitunnel_api_key,
            settings.smtp_password,
            settings.tg_api_hash,
            settings.tg_session_path,
        ]
    redactor = SecretRedactor(secret_values)
    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(name)s %(message)s",
        )
    for handler in root.handlers:
        handler.addFilter(redactor)
    root.addFilter(redactor)
