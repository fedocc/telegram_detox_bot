#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

if (( $# > 1 )); then
    echo "Usage: $0 [project-root]" >&2
    exit 1
fi

# The optional root is used by the deploy workflow when this verified script is
# streamed from the target revision before git pull. Normal operator use derives
# the same directory from the installed script path.
if (( $# == 1 )); then
    root_dir="$(cd -- "$1" && pwd -P)"
else
    root_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
fi
python_bin="${root_dir}/.venv/bin/python"

if [[ ! -x "${python_bin}" ]]; then
    echo "Project virtualenv Python is missing or not executable." >&2
    exit 1
fi

cd -- "${root_dir}"
"${python_bin}" - "${root_dir}" <<'PY'
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.engine import make_url

from app.config import Settings


def abort(message: str) -> None:
    raise SystemExit(message)


def check_integrity(connection: sqlite3.Connection, label: str) -> None:
    result = connection.execute("PRAGMA integrity_check").fetchall()
    if result != [("ok",)]:
        abort(f"{label} failed SQLite integrity_check; no backup was published.")


root = Path(sys.argv[1]).resolve(strict=True)
try:
    url = make_url(Settings().database_url)
except Exception:
    abort("DATABASE_URL is invalid; no backup was created.")

if url.get_backend_name() != "sqlite":
    abort("DATABASE_URL must identify a file-backed SQLite database.")
if any(value is not None for value in (url.username, url.password, url.host, url.port)):
    abort("SQLite DATABASE_URL must not contain authority components.")
if url.query:
    abort("SQLite DATABASE_URL query parameters are not supported by the backup workflow.")
if not url.database or url.database == ":memory:" or url.database.startswith("file:"):
    abort("DATABASE_URL must identify an unambiguous SQLite database file.")

database = Path(url.database)
if not database.is_absolute():
    database = root / database
try:
    database = database.resolve(strict=True)
except (FileNotFoundError, OSError, RuntimeError):
    abort("The SQLite database configured by DATABASE_URL does not exist.")
if not database.is_file():
    abort("The SQLite path configured by DATABASE_URL is not a regular file.")

backup_dir = root / "backups"
if backup_dir.is_symlink():
    abort("Refusing to use a symlink as the SQLite backup directory.")
backup_dir.mkdir(mode=0o700, parents=False, exist_ok=True)
if not backup_dir.is_dir():
    abort("The SQLite backup path is not a directory.")
backup_dir.chmod(0o700)

timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
backup_path = backup_dir / f"telegram_digest_{timestamp}.sqlite"
descriptor, temporary_name = tempfile.mkstemp(
    prefix=".telegram_digest_", suffix=".sqlite.tmp", dir=backup_dir,
)
os.close(descriptor)
temporary_path = Path(temporary_name)
temporary_path.chmod(0o600)

try:
    source_uri = f"{database.as_uri()}?mode=ro"
    with closing(sqlite3.connect(source_uri, uri=True, timeout=30)) as source:
        check_integrity(source, "Source database")
        with closing(sqlite3.connect(temporary_path, timeout=30)) as destination:
            source.backup(destination)
            destination.commit()
            check_integrity(destination, "Backup")
    os.replace(temporary_path, backup_path)
    backup_path.chmod(0o600)
except BaseException:
    temporary_path.unlink(missing_ok=True)
    raise

print(f"SQLite backup created and verified: {backup_path}")
PY
