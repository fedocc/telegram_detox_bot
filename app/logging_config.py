from __future__ import annotations

import logging
import re

SECRET_PATTERNS = [
    re.compile(r"(AITUNNEL_API_KEY|SMTP_PASSWORD|TG_API_HASH)=([^,\s]+)", re.I),
    re.compile(
        r"(api[_-]?key|password|api[_-]?hash|session)"
        r"(['\"]?\s*[:=]\s*['\"]?)([^'\"\s,]+)",
        re.I,
    ),
]


class SecretRedactor(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        for pattern in SECRET_PATTERNS:
            if pattern.groups >= 3:
                message = pattern.sub(r"\1\2[REDACTED]", message)
            else:
                message = pattern.sub(r"\1=[REDACTED]", message)
        record.msg = message
        record.args = ()
        return True


def configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger().addFilter(SecretRedactor())
