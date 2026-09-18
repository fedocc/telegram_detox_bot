#!/usr/bin/env bash
set -Eeuo pipefail

backend="http://127.0.0.1:8787"

if ! command -v tailscale >/dev/null 2>&1; then
    echo "tailscale is not installed." >&2
    exit 1
fi

curl --fail --silent --show-error "${backend}/api/health" >/dev/null

dns_name="$(tailscale status --json | python3 -c '
import json, sys
payload = json.load(sys.stdin)
name = str(payload.get("Self", {}).get("DNSName", "")).rstrip(".")
state = payload.get("BackendState")
if state != "Running" or not name.endswith(".ts.net"):
    raise SystemExit("Tailscale is not connected with MagicDNS")
print(name)
')"

assert_no_funnel() {
    tailscale serve status --json | python3 -c '
import json, sys
payload = json.load(sys.stdin)

def funnel_enabled(value):
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "AllowFunnel" and child:
                return True
            if funnel_enabled(child):
                return True
    elif isinstance(value, list):
        return any(funnel_enabled(child) for child in value)
    return False

if funnel_enabled(payload):
    raise SystemExit("Refusing deployment: Tailscale Funnel is enabled")
'
}

# Refuse before changing any Serve state, and verify again afterwards. We do not
# reset another service's configuration automatically.
assert_no_funnel

# --bg persists across tailscaled and machine restarts. Serve is tailnet-only;
# this script intentionally never invokes the public-sharing command.
tailscale serve --bg 8787 >/dev/null
assert_no_funnel

url="https://${dns_name}"
echo "Tailscale Serve is private at ${url}"
echo "Set this exact value, then restart the service:"
echo "INBOX_ALLOWED_ORIGINS=http://127.0.0.1:8787,${url}"
