# macOS inbox notifier

The private Aeza listener records an attention event in the same transaction as
conversation activation (mention, direct reply, or an ordinary private message from a
human account). `GET /api/notifications?after=N` returns at most 100
scanned events with a cursor. Without `after`, it returns only the latest cursor.
Reading this feed, sidebar, or local status never opens a conversation.

A SQLite AUTOINCREMENT event ID and unique `(peer_id, trigger_id)` constraint
provide stable dedup across backend restarts. Closed, expired and ignored
conversations are suppressed; the cursor still advances over them. Notification
text is redacted after 24 hours; compact IDs remain as dedup tombstones. The feed
contains only event ID, conversation ID, title, topic title, preview, reason,
local unread count and timestamp. Opening a conversation suppresses its already-seen
rows without deleting notification history or moving the feed cursor.

The Swift app uses AppKit, Network and UserNotifications from macOS, with no
third-party runtime or Homebrew dependencies. It polls the fixed localhost feed
using ephemeral URLSession, bypasses proxies, rejects redirects, and never loads
Telegram sessions, environment files, Gmail tokens or SSH keys. It stores only
cursor, bootstrap policy, enabled state and an optional snooze cutoff in an atomically replaced mode-600
`~/Library/Application Support/TelegramMentionInbox/notifier_state.json`.
A local flock prevents duplicate instances. The bridge token stays in memory.

First launch bootstraps to the latest position. OFF and a 10-minute through
12-hour snooze continue consuming without banners. OFF→ON bootstraps again;
manual or natural snooze expiry retains a persisted cutoff, so neither restart,
sleep nor an in-flight page can produce a muted backlog. Normal reconnect resumes
from the saved cursor; events older than 15 minutes are silently consumed.
Requests time out and back off to at most 60 seconds; logs record connection state
changes rather than every failed poll.

Delivery deliberately uses **at-most-once submission**: cursor is saved before
submitting to UserNotifications, with one stable native request ID per conversation.
Events in one fetched page are coalesced to the newest preview/count. Before a
replacement the app removes pending and delivered items with that identifier. A crash between
that save and OS submission can lose a banner; it cannot replay the event after
restart. Exact-once display across SQLite, the Mac filesystem and macOS notification
center is not transactional. Inbox pending attention and email delivery remain
independent of banners. macOS permissions and Focus settings determine visibility.

Clicking a banner opens the fixed inbox URL with a validated 32-character
conversation ID. The frontend consumes this explicit link once and calls POST
`/open`; closed/expired/invalid IDs fall back to the ordinary inbox.

## Backend deployment

From the Mac, connect using the existing SSH access. In `/opt/telegram-detox`:

```sh
runuser -u telegram-detox -- git pull --ff-only origin main
systemctl restart telegram-detox.service
runuser -u telegram-detox -- .venv/bin/python -m app.cli.healthcheck
runuser -u telegram-detox -- .venv/bin/python -m app.cli.security_check
curl --fail http://127.0.0.1:8787/api/health
curl --fail http://127.0.0.1:8787/api/notifications
ss -ltnp 'sport = :8787'
journalctl -u telegram-detox.service --since '3 minutes ago' --no-pager
```

`init_db` adds the notification table at startup. No new production dependencies.
The feed bootstrap response has no message previews. Do not print the complete
feed with `after=0` during production diagnostics.

## Mac install

After the backend feed is deployed, from the repository root:

```sh
tools/macos/notifier/install.sh
launchctl print gui/$(id -u)/com.fedocc.telegram-inbox-notifier
tools/macos/notifier_status.sh
```

The build needs Apple's Swift compiler (`xcrun swiftc`, Xcode or Command Line Tools).
The install registers a signed local `.app` bundle, installs its LaunchAgent with
RunAtLoad/KeepAlive and a 15-second throttle, then bootstraps it without a Terminal
window. Allow notifications for **Telegram Inbox Notifier** when macOS asks. If
denied, enable them under System Settings → Notifications. A locally ad-hoc signed
bundle is intended for this Mac, not redistribution. Rebuilding/reinstalling may
require reviewing macOS notification permission again.

The installer only manages `com.fedocc.telegram-inbox-notifier`. It does not touch
`com.fedocc.telegram-inbox-tunnel`, shell profiles, SSH configuration or credentials.
Logs: `~/Library/Logs/telegram-inbox-notifier.log` and
`~/Library/Logs/telegram-inbox-notifier-error.log`. No message text is logged.

## Toggle and CLI

The sidebar control calls the Mac bridge at `127.0.0.1:8788`. Exact Host and Origin
`http://127.0.0.1:8787` are required, with explicit CORS and no wildcard. A random
process-scoped token from GET `/status` is required for JSON POST `/enable`,
`/disable`, `/snooze` and `/conversation-opened`. Snooze accepts only the six fixed
durations; conversation IDs are validated before native removal. Other routes and
methods are denied. The bridge has bounded request
size, connection count and timeouts, and cannot execute commands or control launchd.
CSP allows this one additional localhost origin. No public listeners are added.

```sh
tools/macos/notifier_on.sh
tools/macos/notifier_off.sh
tools/macos/notifier_status.sh
```

OFF suppresses banners while keeping the LaunchAgent, tunnel, inbox, email alerts,
birthdays and Telegram listener running. Repeated ON/OFF calls are idempotent.
When the bridge is unavailable the switch is disabled; failed changes roll back
visually. OS permission denial is shown separately from enabled state.

To uninstall this helper manually (state is retained):

```sh
launchctl bootout gui/$(id -u)/com.fedocc.telegram-inbox-notifier
rm "$HOME/Library/LaunchAgents/com.fedocc.telegram-inbox-notifier.plist"
```

## Tests and isolated native QA

```sh
.venv/bin/python -m pytest
node --test tests/*.test.mjs
tools/macos/notifier/test.sh
tools/macos/notifier/build.sh
.venv/bin/ruff check .
git diff --check
```

`build.sh --qa` creates a separate `Telegram Inbox Notifier QA.app`. Its compiled
endpoints are 8877/8878 and its state directory is `/tmp/TelegramMentionInbox-QA`;
it never contacts the production tunnel. Start its synthetic server using:

```sh
.venv/bin/python -m tools.macos.notifier.qa_server
```

The synthetic server exposes POST `/qa/event` with a numeric `id`, protected by
its session CSRF token and exact Origin. This route does not exist in production.
Use it to verify OFF, ON, repeat ID, restart and native click handling without
sending a Telegram message or email. QA state, binaries, generated plists and logs
are not committed. Remove the temporary QA LaunchAgent after testing.
