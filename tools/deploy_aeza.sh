#!/bin/bash
# Run from the Mac where the existing SSH key works. No credentials are read or copied.
set -euo pipefail
repo="$(cd "$(dirname "$0")/.." && pwd)"
expected=$(git -C "$repo" rev-parse HEAD)
test -z "$(git -C "$repo" status --porcelain)"
ssh -i "$HOME/.ssh/aeza_tg_detox_ed25519" root@45.80.228.215 bash -s -- "$expected" <<'REMOTE'
set -euo pipefail
cd /opt/telegram-detox
expected="$1"
test -z "$(runuser -u telegram-detox -- git status --porcelain)"
test "$(runuser -u telegram-detox -- git branch --show-current)" = main
runuser -u telegram-detox -- git fetch origin
test "$(runuser -u telegram-detox -- git rev-parse origin/main)" = "$expected"
runuser -u telegram-detox -- git pull --ff-only origin main
test "$(runuser -u telegram-detox -- git rev-parse HEAD)" = "$expected"
systemctl restart telegram-detox.service
curl --fail --silent --show-error --retry 15 --retry-delay 2 --retry-connrefused \
  http://127.0.0.1:8787/api/health
echo
runuser -u telegram-detox -- .venv/bin/python -m app.cli.healthcheck
runuser -u telegram-detox -- .venv/bin/python -m app.cli.security_check
runuser -u telegram-detox -- .venv/bin/python - <<'PY'
from app.config import get_settings
from app.ignored_chats import load_ignored_chats_from_settings
from app.inbox.store import ACTIVE_MINUTES
s = get_settings()
assert s.mention_only_mode and s.inbox_enabled
assert len(load_ignored_chats_from_settings(s).chat_ids) == 7
assert ACTIVE_MINUTES == 5
print('deterministic=True; LLM=False; digest=False; ignored=7; active_minutes=5')
print('birthdays=', s.birthday_reminders_enabled)
PY
curl --fail --silent --show-error http://127.0.0.1:8787/api/notifications
echo
listeners=$(ss -H -ltn 'sport = :8787' | awk '{print $4}')
test "$listeners" = '127.0.0.1:8787'
systemctl is-active telegram-detox.service
systemctl status telegram-detox.service --no-pager -l
journalctl -u telegram-detox.service --since '3 minutes ago' --no-pager
printf 'Deployed %s; review runtime logs above for repeated errors.\n' "$expected"
REMOTE
