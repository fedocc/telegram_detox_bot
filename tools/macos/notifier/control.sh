#!/bin/bash
set -euo pipefail
action="${1:-status}"
case "$action" in status|enable|disable) ;; *) exit 2 ;; esac
origin='http://127.0.0.1:8787'
bridge='http://127.0.0.1:8788'
status=$(/usr/bin/curl --noproxy '*' -fsS --max-time 4 -H "Origin: $origin" "$bridge/status")
if [ "$action" != status ]; then
  csrf=$(printf '%s' "$status" | /usr/bin/plutil -extract csrf raw -o - -)
  status=$(/usr/bin/curl --noproxy '*' -fsS --max-time 4 -H "Origin: $origin" \
    -H 'Content-Type: application/json' -H "X-Notifier-CSRF: $csrf" \
    --data '{}' "$bridge/$action")
fi
enabled=$(printf '%s' "$status" | /usr/bin/plutil -extract enabled raw -o - -)
if [ "$enabled" = true ]; then printf 'Telegram notifier: ON\n'; else printf 'Telegram notifier: OFF\n'; fi
