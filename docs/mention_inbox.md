# Telegram Mention Inbox

The inbox runs inside `telegram-detox.service`. The listener owns the only Telethon
user session; aiohttp shares that client and event loop. Never start a second client
against the production session. The web interface is enabled by default in mention-only
mode (`INBOX_ENABLED=false` disables only the UI). LLM, digest and startup backfill stay
disabled in that mode; birthday jobs remain independent.

## Private access

aiohttp always binds `127.0.0.1:8787`. The existing Mac path is an SSH tunnel:

```sh
ssh -i ~/.ssh/aeza_tg_detox_ed25519 \
  -L 8787:127.0.0.1:8787 \
  root@45.80.228.215
```

Open **http://127.0.0.1:8787**. The configured Origin and Host must match exactly.
Do not bind the service to `0.0.0.0`, expose port 8787, add an unauthenticated public
reverse proxy, or enable Tailscale Funnel: the web UI acts through an already authenticated
Telegram session and has no separate application login.

For iPhone, install Tailscale on the VPS and phone, join the same tailnet, and run on
the VPS:

```sh
cd /opt/telegram-detox
sudo ./deploy/configure_tailscale_serve.sh
```

The script configures tailnet-only Serve, refuses Funnel, and prints the exact HTTPS
origin. Add it without a wildcard and restart the service:

```env
INBOX_ALLOWED_ORIGINS=http://127.0.0.1:8787,https://<node>.<tailnet>.ts.net
```

On iPhone open that HTTPS URL in Safari, then choose **Share → Add to Home Screen**.
The manifest uses standalone mode and the service worker caches only the static shell;
`/api/*`, Telegram media, messages, pins and search results remain network-only and
`no-store`. The local Mac bridge on `127.0.0.1:8788` is hidden on mobile and is never
served over Tailscale.

The alternative iPhone path uses a named Cloudflare Tunnel whose exact custom hostname is
protected first by a deny-by-default Cloudflare Access application. Its only Allow include
must be the operator's explicit email; never use Quick Tunnel, Everyone, Bypass, or a public
policy. After the Access application and named tunnel exist, configure the server with:

```sh
sudo ./deploy/configure_cloudflare_tunnel.sh <hostname> <tunnel-uuid> <credentials-json>
```

Then set the exact values and restart:

```env
INBOX_CLOUDFLARE_ORIGIN=https://<hostname>
INBOX_ALLOWED_ORIGINS=http://127.0.0.1:8787,https://<hostname>
```

Web Push requires a VAPID secret generated only on production:

```sh
sudo -u telegram-detox .venv/bin/python deploy/configure_web_push.py /opt/telegram-detox
```

The private key stays only in the mode-`600` production `.env`. The frontend receives the
public key. Subscription endpoints and `auth`/`p256dh` keys are stored server-side and are
never logged. iOS permission is requested only after the user opens the installed Home
Screen app and taps the notification control. Each push shows a visible notification,
groups by conversation, and deep-links only to a validated local conversation identifier.

## Canonical conversations and local read state

Inbox identity is `(peer_type, marked_peer_id, thread_id)`. A private peer always has
`thread_id=0`; reply metadata in a private chat can never create a phantom conversation.
Ordinary groups remain peer-wide, real forum topics retain their topic root, and
discussion roots keep their existing semantics. SQLite enforces the identity and the
idempotent migration merges legacy duplicates while repointing send/notification rows.

An activation stores the event's peer type and access hash server-side. History, exact
messages, search, pins, media and sends resolve an `InputPeer` from that identity. On a
peer-invalid Telegram response the backend performs one bounded dialog re-resolution.
A repeated permanent failure quarantines only that projection and returns 410; the
frontend removes its selection and does not poll it every two seconds. Access hashes are
never serialized to the browser or logs; browser routes resolve only server-allowlisted
conversation/source identifiers.

Each durable conversation records `latest_relevant_message_id`,
`last_seen_message_id`, and `unread_count`. Sidebar/history/notification polling does
not change them. Only the CSRF-protected `POST /open` caused by a user selection marks
the current batch locally seen. This is app-local state: runtime code must never call
`send_read_acknowledge`, `ReadHistoryRequest`, or another Telegram read-receipt API.

A new projection is pending without a countdown. Its first actual open starts the
five-minute window; repeated opens do not extend it. A successful manual send or a new
meaningful trigger while opened preserves the existing extension behavior. Close or
expiry hides the projection but keeps its canonical row and dedup state, so a later
message reuses the stable conversation ID.

Incoming human private messages, exact configured mentions, and direct replies create
Inbox attention. A selected Library source also creates one peer-wide temporary Inbox
projection for its incoming messages. Ten messages still produce one row with a count,
and opening it clears the local count; closing it never removes the Library source.

## Library

The first source is always ☆ **Избранное** (Telegram Saved Messages). Other sources are
selected in the Library management drawer. Telegram dialogs are fetched only when that
drawer is explicitly opened. The browser receives short-lived opaque tokens; it cannot
submit a peer ID or access hash.

Selections, fixed order, local notification mute, bot-write permission,
`digest_excluded`, and per-source seen state live in SQLite. The old
`data/library_chats.json` is only an idempotent startup seed for compatibility; it is no
longer the primary control surface and never triggers a Telegram dialog scan. Deselecting
a source revokes its history/search/media routes and closes its Inbox projection.

Ordinary Library groups, channels and people are read-only. An explicitly selected peer
may be writable only when Telegram currently resolves it as a bot and
`allow_bot_write=true`; text, photo and generic-file sends still require a manual click.
The Inbox projection route cannot bypass that check. Избранное remains writable and uses
the existing delayed server scheduling.

Each source loads the latest 50 messages. “Загрузить предыдущие сообщения” sends the
oldest loaded ID as exclusive `offset_id`, prepends the next page, deduplicates IDs and
preserves the visible scroll anchor. The control disappears when Telegram returns no
older page.

Pins use Telegram's real pinned-message filter and a bounded 60-second cache, independent
of the current history page. Search uses Telegram server-side search only for the current
validated source/conversation, 30 results at a time; there is no global/local full-history
index. Clicking a pin or result fetches that exact message if needed and updates the
stable deep link:

```text
/?conversation=<32-hex-conversation-id>
/?library=<opaque-source-id>
/?library=<opaque-source-id>&message=<positive-telegram-message-id>
```

The backend verifies that the source is enabled and that an exact Inbox message belongs
to its canonical peer/topic.

## Rendering, history and media

Telegram `MessageEntityTextUrl` and `MessageEntityUrl` offsets are decoded as UTF-16 code
units. The API returns text/link segments; the frontend builds text nodes and anchors
with only `http`/`https`, `target="_blank"`, and `rel="noopener noreferrer"`. Telegram
HTML is never injected. The same renderer is used for Inbox, Library, Избранное, replies,
pins and search results.

Inbox context is held only in memory: up to 500 messages per conversation and 20 recently
opened conversations. Library history is explicit and paged. Photos, supported video and
audio render inline; other files download as attachments. A source/conversation must be
allowlisted, exact messages are checked, individual downloads are capped at 64 MiB, cache
size is capped at 256 MiB, partial files are removed, and generated cache files use mode
600. Uploaded files are streamed into a private temporary directory with bounded size and
concurrency and are removed after success or failure.

## Manual sending and web security

The server derives every target from an active Inbox conversation or enabled Library
source. There is no arbitrary username, peer, New Chat, forward, reaction, edit, delete,
pin/unpin, join/leave, or autonomous send endpoint. Request IDs make manual sends
idempotent; ambiguous delivery is not retried automatically and drafts remain in
`sessionStorage`.

Mutating requests require the exact configured Origin, an in-memory session CSRF token,
same-site Fetch Metadata, and JSON or bounded multipart content. Host is validated against
the same exact origin set. CSP, no-store API responses, disabled access logs and sanitized
exception logging prevent private payload leakage. Allowed HTTPS origins must be exact
`.ts.net` names; wildcards and public HTTP origins are rejected.

## Native Mac notifications

The optional [Swift notifier](../tools/macos/notifier/README.md) works without an open
browser. It polls while muted so its cursor advances, coalesces pending events by stable
`conversation:<id>` identifier, removes a previously delivered item before submitting its
replacement, and removes the item when the conversation opens. Notification history in
SQLite is retained for cursor/dedup purposes.

The loopback menu preserves permanent ON/OFF and adds persistent snooze for 10/30 minutes
or 1/3/6/12 hours. During snooze it suppresses banners but continues polling; enable-now,
restart and sleep/wake do not replay a backlog. The bridge remains bound to loopback with
its existing Origin/token checks.

## Database and operation

Migrations are additive and idempotent. They preserve birthday/digest tables, pending and
opened attention state, notification cursors, send idempotency, and Library preferences.
Expired conversation identity and notification tombstones remain durable; old notification
text is redacted. Before deploying a migration, create the restrictive SQLite backup. The
script resolves the runtime `DATABASE_URL`, refuses ambiguous/non-SQLite targets, and only
publishes the mode-`600` copy after both databases pass `integrity_check`:

```sh
sudo -u telegram-detox /opt/telegram-detox/deploy/backup_sqlite.sh
```

Validation is entirely mock-backed and must run without a second Telegram client:

```sh
.venv/bin/python -m pytest
.venv/bin/python -m ruff check .
node --test tests/*.test.mjs
tools/macos/notifier/test.sh
git diff --check
```

On production, verify `/api/health`, `/api/notifications`, `/api/library`, the security
check, `127.0.0.1:8787` listener, Telegram connection, Serve-without-Funnel status, and
recent systemd logs. Do not print conversation content or credentials while inspecting
the service.
