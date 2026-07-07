from __future__ import annotations

import subprocess
from pathlib import Path


def test_env_and_session_files_are_ignored_by_git() -> None:
    root = Path(__file__).resolve().parents[1]
    targets = [".env", "data/telegram_digest.session", "data/telegram_digest.db", "logs/app.log"]
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
