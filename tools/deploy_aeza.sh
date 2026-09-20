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
# The running revision owns the pre-migration schema. Execute the backup logic from
# the already verified target SHA: on the first deploy the checkout still contains
# the older hard-coded script until after this backup and the subsequent pull.
runuser -u telegram-detox -- git show "${expected}:deploy/backup_sqlite.sh" \
  | runuser -u telegram-detox -- bash -s -- /opt/telegram-detox
runuser -u telegram-detox -- git pull --ff-only origin main
test "$(runuser -u telegram-detox -- git rev-parse HEAD)" = "$expected"
runuser -u telegram-detox -- .venv/bin/python -m pip install --disable-pip-version-check \
  'pip>=26.2,<27'
runuser -u telegram-detox -- .venv/bin/python -m pip install --disable-pip-version-check -e .
systemctl restart telegram-detox.service
curl --fail --silent --show-error --retry 15 --retry-delay 2 --retry-connrefused \
  http://127.0.0.1:8787/api/health
echo
runuser -u telegram-detox -- .venv/bin/python -m app.cli.healthcheck
runuser -u telegram-detox -- .venv/bin/python -m app.cli.security_check
runuser -u telegram-detox -- .venv/bin/python - <<'PY'
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.config import get_settings
from app.ignored_chats import load_ignored_chats_from_settings
from app.inbox.store import ACTIVE_MINUTES
s = get_settings()
assert s.mention_only_mode and s.inbox_enabled
assert len(load_ignored_chats_from_settings(s).chat_ids) == 7
assert ACTIVE_MINUTES == 5
assert s.digest_enabled and s.digest_time == '07:00'
assert s.digest_model == 'gemini-3.8-flash'
assert bool(s.gemini_api_key)
zone = ZoneInfo(s.timezone)
now = datetime.now(zone)
hour, minute = (int(part) for part in s.digest_time.split(':', 1))
next_digest = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
if next_digest <= now:
    next_digest += timedelta(days=1)
print('deterministic=True; ignored=7; active_minutes=5')
print('digest_enabled=True; digest_time=07:00; digest_model=gemini-3.8-flash')
print('gemini_api_key_configured=yes')
print('next_scheduled_digest=', next_digest.isoformat())
print('birthdays=', s.birthday_reminders_enabled)
PY
curl --fail --silent --show-error http://127.0.0.1:8787/api/notifications
echo
curl --fail --silent --show-error http://127.0.0.1:8787/api/library | python3 -c '
import json, sys
payload = json.load(sys.stdin)
sources = payload.get("sources")
assert isinstance(sources, list) and sources and sources[0].get("id") == "saved"
print("library_ok=True; source_count=", len(sources))
'
listeners=$(ss -H -ltn 'sport = :8787' | awk '{print $4}')
test "$listeners" = '127.0.0.1:8787'
printf 'listener=%s\n' "$listeners"
systemctl is-active telegram-detox.service
printf 'production_sha=%s\n' "$(runuser -u telegram-detox -- git rev-parse HEAD)"

# Startup reconciliation runs immediately. Wait for the canonical digest or an
# explicit pending-retry log; never create or alter digest state from deployment.
digest_state=null
for _ in $(seq 1 32); do
  digest_payload=$(curl --fail --silent --show-error http://127.0.0.1:8787/api/digest/latest)
  digest_state=$(python3 -c 'import json,sys; print("ready" if json.load(sys.stdin).get("digest") else "null")' \
    <<<"$digest_payload")
  test "$digest_state" = ready && break
  grep -q 'morning_digest pending retry' \
    < <(journalctl -u telegram-detox.service --since '10 minutes ago' --no-pager) && break
  sleep 15
done
printf 'today_digest_status=%s\n' "$digest_state"
recent_logs=$(journalctl -u telegram-detox.service --since '10 minutes ago' --no-pager)
printf '%s\n' "$recent_logs"
printf 'recent_log_counts crash=%s http_500=%s peer_id_invalid=%s digest_pending_retry=%s\n' \
  "$(grep -Eci 'Traceback|crash loop' <<<"$recent_logs" || true)" \
  "$(grep -Eci 'HTTP 500| 500 ' <<<"$recent_logs" || true)" \
  "$(grep -ci 'PeerIdInvalidError' <<<"$recent_logs" || true)" \
  "$(grep -ci 'morning_digest pending retry' <<<"$recent_logs" || true)"
systemctl status telegram-detox.service --no-pager -l
printf 'Deployed %s; review runtime logs above for repeated errors.\n' "$expected"
REMOTE
