from __future__ import annotations

import stat
import sys
from collections.abc import Callable
from pathlib import Path

from app.config import Settings, get_settings


def _mode(path: Path) -> int | None:
    try:
        return stat.S_IMODE(path.stat().st_mode)
    except FileNotFoundError:
        return None


def check_security(settings: Settings, *, env_path: Path = Path(".env")) -> list[str]:
    errors: list[str] = []
    files = [
        (env_path, ".env"),
        (settings.tg_session_path, "Telegram session"),
        (settings.gmail_oauth_token_path, "Gmail OAuth token"),
        (settings.gmail_oauth_client_secret_path, "Gmail OAuth client JSON"),
    ]
    for path, label in files:
        mode = _mode(path)
        if mode is None:
            errors.append(f"{label} is missing.")
        elif mode != 0o600:
            errors.append(f"{label} must have mode 600.")

    directories = [
        (settings.tg_session_path.parent, "data directory"),
        (settings.gmail_oauth_client_secret_path.parent, "secrets directory"),
    ]
    for path, label in directories:
        mode = _mode(path)
        if mode is None:
            errors.append(f"{label} is missing.")
        elif mode != 0o700:
            errors.append(f"{label} must have mode 700.")
    return errors


def run(
    settings: Settings,
    *,
    env_path: Path = Path(".env"),
    output: Callable[[str], None] = print,
) -> bool:
    errors = check_security(settings, env_path=env_path)
    if errors:
        output("Security check failed:")
        for error in errors:
            output(f"- {error}")
        return False
    output("Security check passed.")
    return True


def main() -> None:
    try:
        settings = get_settings()
    except Exception:
        print("Security check failed: configuration cannot be loaded.", file=sys.stderr)
        raise SystemExit(1) from None
    if not run(settings):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
