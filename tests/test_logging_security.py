from __future__ import annotations

import io
import logging

from app.config import Settings
from app.logging_config import configure_logging


def test_secret_redaction_applies_to_child_loggers() -> None:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    root = logging.getLogger()
    root.handlers = [handler]

    settings = Settings.model_validate(
        {"aitunnel_api_key": "sk-" + "secret", "smtp_password": "smtp-" + "secret"}
    )
    configure_logging(settings)
    logging.getLogger("app.child").warning("%s", "AITUNNEL_" + "API_KEY=sk-secret")

    assert "sk-secret" not in stream.getvalue()
    assert "[REDACTED]" in stream.getvalue()


def test_secret_redaction_applies_to_exception_logging() -> None:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    root = logging.getLogger()
    root.handlers = [handler]

    configure_logging(Settings.model_validate({"tg_api_hash": "hash-" + "secret"}))
    try:
        raise RuntimeError("TG_" + "API_HASH=hash-secret")
    except RuntimeError:
        logging.getLogger("app.child").exception("failed")

    assert "hash-secret" not in stream.getvalue()


def test_log_output_never_contains_configured_secret_values() -> None:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    root = logging.getLogger()
    root.handlers = [handler]
    settings = Settings.model_validate(
        {
            "aitunnel_api_key": "key-" + "secret",
            "smtp_password": "mail-" + "secret",
            "tg_api_hash": "hash-" + "secret",
            "tg_session_path": "data/private" + ".session",
        }
    )

    configure_logging(settings)
    logging.getLogger("app").error(
        "values %s %s %s %s",
        settings.aitunnel_api_key,
        settings.smtp_password,
        settings.tg_api_hash,
        settings.tg_session_path,
    )

    output = stream.getvalue()
    assert "key-secret" not in output
    assert "mail-secret" not in output
    assert "hash-secret" not in output
    assert "private.session" not in output
