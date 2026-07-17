#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
database_path="${root_dir}/data/telegram_digest.db"
backup_dir="${root_dir}/backups"
timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
backup_path="${backup_dir}/telegram_digest_${timestamp}.sqlite"
temporary_path="${backup_path}.tmp"

if ! command -v sqlite3 >/dev/null 2>&1; then
    echo "sqlite3 is required for backups." >&2
    exit 1
fi
if [[ ! -f "${database_path}" ]]; then
    echo "SQLite database file is missing." >&2
    exit 1
fi

mkdir -p "${backup_dir}"
chmod 700 "${backup_dir}"
trap 'rm -f "${temporary_path}"' EXIT

sqlite3 "${database_path}" ".backup '${temporary_path}'"
mv "${temporary_path}" "${backup_path}"
trap - EXIT
chmod 600 "${backup_path}"
echo "SQLite backup created: ${backup_path}"
