from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from urllib.parse import quote
from urllib.request import urlopen
from zoneinfo import ZoneInfo

from app.config import Settings
from app.inbox.digest import (
    GeminiDigestProvider,
    approximate_tokens,
    generate_payload,
    preprocess,
    validate_refs,
)

DEFAULT_START = "2026-09-18T07:00:00"
DEFAULT_END = "2026-09-19T07:00:00"
DEFAULT_TIMEZONE = "Europe/Moscow"


def api_json(base_url: str, path: str) -> dict:
    url = f"{base_url.rstrip('/')}{path}"
    with urlopen(url, timeout=30) as response:  # noqa: S310 - operator supplies local URL.
        return json.load(response)


def collect_rows(base_url: str, start: datetime, end: datetime) -> tuple[list[dict], int]:
    sources = [
        source for source in api_json(base_url, "/api/library")["sources"]
        if not source.get("digest_excluded", False)
    ]
    rows = []
    for source in sources:
        before = None
        for _ in range(200):
            suffix = f"?before={before}" if before else ""
            path = f"/api/library/{quote(source['id'], safe='')}/messages{suffix}"
            page = api_json(base_url, path)
            messages = page.get("messages", [])
            for message in messages:
                timestamp = datetime.fromisoformat(message["timestamp"])
                if start <= timestamp.astimezone(start.tzinfo) <= end:
                    media = message.get("media") or {}
                    rows.append({
                        "source_id": source["id"],
                        "source_title": source["title"],
                        "message_id": message["id"],
                        "timestamp": message["timestamp"],
                        "sender": message.get("sender", ""),
                        "text": message.get("text", ""),
                        "service": bool(message.get("system")),
                        "sticker_only": (
                            media.get("kind") == "sticker" and not message.get("text")
                        ),
                    })
            if not messages or not page.get("next_before"):
                break
            oldest = min(
                datetime.fromisoformat(message["timestamp"]).astimezone(start.tzinfo)
                for message in messages
            )
            if oldest < start:
                break
            before = page["next_before"]
        else:
            raise RuntimeError("QA pagination limit reached")
    return rows, len(sources)


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only Morning Digest QA")
    parser.add_argument("--base-url", default="http://127.0.0.1:8787")
    parser.add_argument("--start", default=DEFAULT_START)
    parser.add_argument("--end", default=DEFAULT_END)
    parser.add_argument("--timezone", default=DEFAULT_TIMEZONE)
    parser.add_argument("--env-file", default=None)
    args = parser.parse_args()

    zone = ZoneInfo(args.timezone)
    start = datetime.fromisoformat(args.start).replace(tzinfo=zone)
    end = datetime.fromisoformat(args.end).replace(tzinfo=zone)
    if end <= start:
        raise RuntimeError("QA end must be after start")

    settings = Settings(_env_file=args.env_file)
    provider = GeminiDigestProvider(settings)
    diagnostics = {
        "model": settings.digest_model,
        "provider": "direct Gemini API",
        "minimal_provider_smoke": "PENDING",
        "structured_output_smoke": "PENDING",
    }
    try:
        provider.smoke_text()
        diagnostics["minimal_provider_smoke"] = "PASS"
    except RuntimeError as exc:
        diagnostics["minimal_provider_smoke"] = "FAIL"
        diagnostics["error"] = str(exc)
        print(json.dumps(diagnostics, ensure_ascii=False, indent=2))
        sys.exit(1)
    try:
        provider.smoke_structured()
        diagnostics["structured_output_smoke"] = "PASS"
    except RuntimeError as exc:
        diagnostics["structured_output_smoke"] = "FAIL"
        diagnostics["error"] = str(exc)
        print(json.dumps(diagnostics, ensure_ascii=False, indent=2))
        sys.exit(1)

    rows, selected_sources = collect_rows(args.base_url, start, end)
    context, refs = preprocess(rows)
    raw_payload = generate_payload(provider, context)
    supplied = set(refs)
    returned_refs = {
        ref for item in raw_payload.items for ref in item.source_refs
    }
    unknown_refs = sorted(returned_refs - supplied)
    payload = validate_refs(raw_payload, supplied)
    items = []
    for item in payload.items:
        value = item.model_dump()
        value["source_refs"] = [
            {"ref": ref, **refs[ref]} for ref in item.source_refs
        ]
        items.append(value)

    print(json.dumps({
        **diagnostics,
        "window": {"start": start.isoformat(), "end": end.isoformat()},
        "selected_library_sources": selected_sources,
        "raw_message_count": len(rows),
        "filtered_deduped_count": len(context),
        "approximate_input_tokens": approximate_tokens(context),
        "generated_digest": {"title": payload.title, "items": items},
        "validation": {
            "json_schema_valid": True,
            "max_six_items": len(payload.items) <= 6,
            "all_returned_refs_were_supplied": not unknown_refs,
            "unknown_refs_rejected": unknown_refs,
            "model_returned_no_links": all(
                set(item.model_dump()) == {"category", "title", "summary", "source_refs"}
                for item in raw_payload.items
            ),
            "facts_dates_details": "operator review required",
        },
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
