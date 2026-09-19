#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

if (( $# != 3 )); then
    echo "Usage: $0 <exact-hostname> <tunnel-uuid> <credentials-json>" >&2
    exit 2
fi

hostname="$1"
tunnel_id="$2"
credentials_source="$3"

if [[ ! "$hostname" =~ ^[A-Za-z0-9][A-Za-z0-9.-]*[A-Za-z0-9]$ ]] \
        || [[ "$hostname" == *..* ]] \
        || [[ "$hostname" == *.trycloudflare.com ]] \
        || [[ "$hostname" != "${hostname,,}" ]]; then
    echo "Refusing: use an exact custom Cloudflare hostname, never a Quick Tunnel." >&2
    exit 1
fi
if [[ ! "$tunnel_id" =~ ^[a-f0-9-]{36}$ ]]; then
    echo "Refusing: invalid tunnel UUID." >&2
    exit 1
fi
if [[ ! -f "$credentials_source" || -L "$credentials_source" ]]; then
    echo "Refusing: credentials must be a regular local file." >&2
    exit 1
fi
command -v cloudflared >/dev/null || {
    echo "cloudflared is not installed. Install it from Cloudflare's signed repository." >&2
    exit 1
}
curl --fail --silent --show-error http://127.0.0.1:8787/api/health >/dev/null

install -d -m 700 -o root -g root /etc/cloudflared
install -m 600 -o root -g root "$credentials_source" "/etc/cloudflared/${tunnel_id}.json"
temporary=$(mktemp /etc/cloudflared/config.yml.XXXXXX)
trap 'rm -f "$temporary"' EXIT
cat >"$temporary" <<EOF
tunnel: ${tunnel_id}
credentials-file: /etc/cloudflared/${tunnel_id}.json
no-autoupdate: true
ingress:
  - hostname: ${hostname}
    service: http://127.0.0.1:8787
  - service: http_status:404
EOF
chmod 600 "$temporary"
mv "$temporary" /etc/cloudflared/config.yml
trap - EXIT

cloudflared --config /etc/cloudflared/config.yml tunnel ingress validate
if systemctl cat cloudflared.service >/dev/null 2>&1; then
    systemctl daemon-reload
    systemctl restart cloudflared
else
    cloudflared --config /etc/cloudflared/config.yml service install
fi
systemctl enable --now cloudflared
systemctl is-active cloudflared
echo "cloudflared is active for the configured hostname."
echo "Verify an unauthenticated request is denied by Cloudflare Access before using the app."
