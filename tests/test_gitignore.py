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
        "data/gmail_oauth_token.json.bak",
        "data/gmail_oauth_token.json.bak-test",
        "data/birthdays.json",
        "data/ignored_chats.json",
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


def test_ignored_chats_example_is_committable_and_private_file_is_ignored() -> None:
    root = Path(__file__).resolve().parents[1]
    example = root / "data" / "ignored_chats.example.json"

    assert example.exists()
    assert subprocess.run(  # noqa: S603
        ["/usr/bin/git", "check-ignore", "data/ignored_chats.example.json"],
        cwd=root,
        capture_output=True,
        check=False,
    ).returncode != 0
    assert subprocess.run(  # noqa: S603
        ["/usr/bin/git", "check-ignore", "data/ignored_chats.json"],
        cwd=root,
        capture_output=True,
        check=False,
    ).returncode == 0
