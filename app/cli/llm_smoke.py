from __future__ import annotations

import argparse
import json
from datetime import date
from typing import Any

from app.config import get_settings
from app.llm.client import (
    HaikuClient,
    LLMError,
    LLMRawTrace,
    sanitize_validation_codes,
    sanitize_validation_error_type,
    sanitize_validation_paths,
)


def synthetic_payload() -> dict[str, Any]:
    texts = [
        "привет бро как дела у тебя?",
        "слушай а ты сможешь сегодня погулять?",
        "завтра в 10 вылет ты готов?",
        "ало ответь",
        "срочно",
    ]
    return {
        "date": date.today().isoformat(),
        "chats": [
            {
                "chat_id": "1001",
                "chat_title": "Synthetic Private",
                "chat_type": "private",
                "messages": [
                    {
                        "message_id": index,
                        "source_ref": {"chat_id": "1001", "message_id": index},
                        "timestamp": f"{date.today().isoformat()}T10:{index:02d}:00+03:00",
                        "sender_name": "Synthetic Sender",
                        "is_outgoing": False,
                        "reply_to_message_id": None,
                        "text": text,
                        "media_type": "none",
                        "alert_sent": False,
                    }
                    for index, text in enumerate(texts, start=1)
                ],
            }
        ],
    }


def _quality_checks(digest) -> dict[str, bool]:
    if not digest.direct_messages:
        return {
            "planning": False,
            "flight": False,
            "answer": False,
            "readiness": False,
            "deadline": False,
            "open_telegram": False,
            "specific_reason": False,
        }
    item = digest.direct_messages[0]
    content = " ".join(
        value
        for value in (
            item.summary,
            item.requests_to_me,
            item.important_context,
            item.action_items,
            item.deadline_text,
            item.open_reason,
        )
        if value
    ).lower()
    reason = (item.open_reason or "").strip().lower()
    return {
        "planning": any(marker in content for marker in ("гуля", "прогул", "план")),
        "flight": any(marker in content for marker in ("вылет", "рейс")),
        "answer": "ответ" in content,
        "readiness": "готов" in content,
        "deadline": "завтра" in content and "10" in content,
        "open_telegram": item.should_open_telegram is True,
        "specific_reason": bool(reason)
        and reason not in {"причина не указана.", "нужно открыть telegram."},
    }


def run(*, show_raw: bool = False, json_output: bool = False) -> int:
    trace = LLMRawTrace()
    result: dict[str, Any]
    try:
        digest = HaikuClient(get_settings()).daily_digest(
            synthetic_payload(),
            raw_trace=trace,
        )
        checks = _quality_checks(digest)
        quality_valid = all(checks.values())
        item = digest.direct_messages[0] if digest.direct_messages else None
        result = {
            "final_valid": quality_valid,
            "llm_used": True,
            "quality_valid": quality_valid,
            "quality_checks": checks,
            "validation_error_type": sanitize_validation_error_type(
                digest.diagnostics.validation_error_type
            ),
            "validation_error_paths": sanitize_validation_paths(
                digest.diagnostics.validation_error_paths
            ),
            "validation_error_codes": sanitize_validation_codes(
                digest.diagnostics.validation_error_codes
            ),
            "repair_attempted": digest.diagnostics.repair_attempted,
            "repair_used": digest.diagnostics.repair_used,
            "summary": item.summary if item else None,
            "requests": item.requests_to_me if item else None,
            "context": item.important_context if item else None,
            "actions": item.action_items if item else None,
            "deadline": item.deadline_text if item else None,
            "open_telegram": item.should_open_telegram if item else None,
            "reason_to_open": item.open_reason if item else None,
        }
    except LLMError as exc:
        result = {
            "final_valid": False,
            "llm_used": False,
            "quality_valid": False,
            "validation_error_type": sanitize_validation_error_type(
                exc.validation_error_type
            ),
            "validation_error_paths": sanitize_validation_paths(
                exc.validation_error_paths
            ),
            "validation_error_codes": sanitize_validation_codes(
                exc.validation_error_codes
            ),
            "repair_attempted": exc.repair_attempted,
            "repair_used": exc.repair_used,
            "fallback_reason": exc.reason_code,
        }

    if show_raw:
        result["raw_initial_output"] = trace.initial_output
        result["raw_repair_output"] = trace.repair_output
    if json_output:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    else:
        if show_raw:
            print("raw_initial_output:")
            print(trace.initial_output or "<not available>")
            print("raw_repair_output:")
            print(trace.repair_output or "<not used>")
        for key, value in result.items():
            if key.startswith("raw_"):
                continue
            if isinstance(value, bool):
                rendered = str(value).lower()
            elif isinstance(value, (dict, list)):
                rendered = json.dumps(value, ensure_ascii=False, sort_keys=True)
            else:
                rendered = "none" if value is None else str(value)
            print(f"{key}={rendered}")
    return 0 if result["final_valid"] and result["llm_used"] else 1


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Call the configured digest LLM with synthetic messages only"
    )
    parser.add_argument(
        "--show-raw",
        action="store_true",
        help="show raw provider output for the built-in synthetic payload only",
    )
    parser.add_argument("--json", action="store_true", help="emit one JSON object")
    args = parser.parse_args(argv)
    raise SystemExit(run(show_raw=args.show_raw, json_output=args.json))


if __name__ == "__main__":
    main()
