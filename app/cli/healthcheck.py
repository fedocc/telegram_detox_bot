from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path

from sqlalchemy import text

from app.config import Settings, get_settings
from app.db.session import make_engine


def _sqlite_database_path(database_url: str) -> Path | None:
    prefix = "sqlite:///"
    if not database_url.startswith(prefix):
        return None
    return Path(database_url.removeprefix(prefix))


def check_health(settings: Settings) -> list[str]:
    errors: list[str] = []
    database_path = _sqlite_database_path(settings.database_url)
    if database_path is None:
        errors.append("SQLite database URL is required for this healthcheck.")
    elif not database_path.is_file():
        errors.append("SQLite database file is missing.")
    else:
        try:
            engine = make_engine(settings)
            with engine.connect() as connection:
                connection.execute(text("SELECT 1"))
            engine.dispose()
        except Exception:
            errors.append("SQLite database is not reachable.")

    if not settings.aitunnel_api_key:
        errors.append("AITunnel API key is not configured.")
    if not settings.tg_session_path.is_file():
        errors.append("Telegram session file is missing.")
    if not settings.gmail_oauth_token_path.is_file():
        errors.append("Gmail OAuth token file is missing.")
    return errors


def run(settings: Settings, *, output: Callable[[str], None] = print) -> bool:
    errors = check_health(settings)
    if errors:
        output("Healthcheck failed:")
        for error in errors:
            output(f"- {error}")
        return False
    output("Healthcheck passed.")
    return True


def main() -> None:
    try:
        settings = get_settings()
    except Exception:
        print("Healthcheck failed: configuration cannot be loaded.", file=sys.stderr)
        raise SystemExit(1) from None
    if not run(settings):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
