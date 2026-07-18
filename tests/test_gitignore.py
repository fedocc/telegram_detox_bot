from __future__ import annotations

import subprocess
from pathlib import Path


def test_env_and_session_files_are_ignored_by_git() -> None:
    root = Path(__file__).resolve().parents[1]
    targets = [
        ".env",
        "data/telegram_digest.session",
        "data/telegram_digest.db",
        "logs/app.log",
        "secrets/google_oauth_client.json",
        "data/gmail_oauth_token.json",
        "data/birthdays.json",
    ]
    result = subprocess.run(  # noqa: S603
        ["/usr/bin/git", "check-ignore", *targets],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    for target in targets:
        assert target in result.stdout


def test_birthday_example_is_committed_and_override_is_ignored() -> None:
    root = Path(__file__).resolve().parents[1]
    example = root / "data" / "birthdays.example.json"

    assert example.exists()
    assert subprocess.run(  # noqa: S603
        ["/usr/bin/git", "check-ignore", "data/birthdays.example.json"],
        cwd=root,
        capture_output=True,
        check=False,
    ).returncode != 0
    assert subprocess.run(  # noqa: S603
        ["/usr/bin/git", "check-ignore", "data/birthdays.json"],
        cwd=root,
        capture_output=True,
        check=False,
    ).returncode == 0
