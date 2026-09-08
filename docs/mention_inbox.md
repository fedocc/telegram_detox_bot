# Telegram Mention Inbox

The inbox runs inside `telegram-detox.service`. The existing listener owns the
only Telethon user session; aiohttp shares its client and asyncio event loop.
It is enabled by default in mention-only mode (`INBOX_ENABLED=false` disables
only the web interface). The email alert and retry path stays enabled. The
runtime does not construct an LLM client or schedule digests/birthdays in this mode.

## Access

Run this on the Mac and keep the SSH connection open:

```sh
ssh -i ~/.ssh/aeza_tg_detox_ed25519 \
  -L 8787:127.0.0.1:8787 \
  root@45.80.228.215
```

Open **http://127.0.0.1:8787**. Use this exact address: Host/Origin validation
intentionally rejects `localhost`, custom domains and alternative ports.
The backend binds only `127.0.0.1:8787`; no firewall or nginx changes are needed.

## Conversation lifecycle

Only incoming, non-self messages matching the existing case-insensitive exact
`@fedocc` matcher activate a conversation. Ignored chats are excluded at ingestion
and again on every API operation. Ordinary messages never activate a conversation.

A `(peer, topic/thread)` conversation lasts 60 minutes from its latest mention.
Close hides it immediately. A successful manual reply extends it for another
60 minutes. A new mention can reopen a closed conversation; a duplicate update
cannot. Reading and ordinary follow-up messages do not extend the deadline.

SQLite adds `inbox_conversations` (routing, timestamps, title, short preview) and
`inbox_sends` (request ID and delivery status, no message bodies). Schema creation
is additive through the existing `init_db`. Closed/expired metadata and media
are cleaned every 30 seconds. Send receipts live for 24 hours to prevent retries
from sending duplicates, including across restarts.

## Context and media

Opening a conversation fetches up to 100 messages preceding/including the mention
and up to 100 following it. Subsequent polling retrieves new messages, including
messages sent from another Telegram client. History is held only in memory:
maximum 500 messages per conversation and 20 recently opened conversations.
Large backlogs catch up in batches of 100. History is never written to SQLite.

Forum topics use their root ID and server-side reply filtering, plus a local
membership check. General topic filters out other topics and scans at most 2,000
preceding messages to collect its context. Discussion replies use the root post
in the discussion peer. Ordinary group replies stay peer-wide. When Telegram
cannot provide the topic title, the UI shows its ID. Deleted/inaccessible messages
or media may be unavailable. Message edits/deletions already in the memory snapshot
are refreshed when that snapshot is discarded (restart or cache eviction).

Photos open larger; videos have native controls; voice/audio has play, seek and
duration controls. Unsupported browser codecs can be downloaded. Files use a
safe download response. Media loads only for active conversations and messages
already opened in that conversation. Downloads are capped at 64 MiB per file and
256 MiB total cache, stored in `data/media_cache` with generated filenames and
mode 600. Nothing under this directory is committed.

## Manual sending and security

The composer sends plain text or a still JPEG/PNG/WebP image on click or ⌘Enter
(Ctrl+Enter also works). Enter alone inserts a newline. Image uploads are limited
to 10 MiB/20 megapixels, decoded with an allowlist, then normalized to JPEG up to
2560px; animations and other file types are rejected. Text is limited to 4096
UTF-16 units, or 1024 with a photo. Text is not parsed as Markdown.

The server derives peer/thread from the active conversation; the browser cannot
supply an arbitrary target. Telethon sends as the existing user account. There is
no voice/video-send endpoint and no autonomous Telegram sending.

Write requests require the exact Origin, a session CSRF header and JSON. Host,
Fetch Metadata, CSP, no-store and same-origin resource checks protect the interface.
No permissive CORS or public listener is configured. HTTP access logs are disabled;
API failures log only exception class names. The SSH tunnel is the access boundary.

Draft text survives page reloads in tab-scoped sessionStorage; attachments remain
in memory until removed/sent or the page closes. Telegram permission failures
preserve the draft. Ambiguous network failures are never retried automatically:
inspect the conversation before composing another send. Repeating the unchanged
request ID returns its prior result or an explicit unknown-status error.

## Validation and operation

```sh
.venv/bin/python -m pytest
.venv/bin/python -m ruff check .
git diff --check
```

All Telegram sends in tests are mocks. The safety scanner permits only the two
explicit send calls inside `InboxService.send`; other runtime writes remain banned.

A synthetic local preview requires no Telegram login or credential files:

```sh
.venv/bin/python -m app.inbox.demo
# Or: .venv/bin/python -m app.inbox.demo --empty
```

Stop the preview before opening the production SSH tunnel, since both use port 8787.
The demo always rejects sending. Its video fixture checks the player layout only.

On the VPS, run health/security/ignored-chat checks as the service user. Check
`/api/health` for the running client's connection state, and summarize the JSON
from `/api/conversations` without logging private conversation contents. Do not
start a diagnostic TelegramClient against the live session.

## Visual reference and checks

The user's Stitch `screen.png`, `DESIGN.md` and `code.html` are the reference.
The screenshot/prototype takes precedence where the design document differs.

| Decision | Reference | Implementation |
|---|---|---|
| Pane sizes | Screenshot / prototype | 240px list, 44px conversation header |
| Canvas and bubbles | Screenshot | Graphite canvas, slate incoming/own, amber mention |
| Typography and density | DESIGN.md | System/Inter stack, 13px text, compact rounded bubbles |
| Composer | Screenshot / user brief | Clip, text, send; selected-image chip above |
| Empty state | User brief | Two lines of text; no illustration |

Local browser QA at 1280×1024 covered active rows, context, photo enlargement,
audio playback/progress, video/file layout, attachment preview/removal, retained
text after a simulated send failure, manual close and the zero-conversation state.
No real Telegram message was sent during validation.
