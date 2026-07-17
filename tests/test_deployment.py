from __future__ import annotations

import io
import stat
from pathlib import Path

from app.cli.healthcheck import run as run_healthcheck
from app.cli.security_check import check_security
from app.db.session import init_db


def _deployment_paths(settings, tmp_path: Path):
    data = tmp_path / "data"
    secrets = tmp_path / "secrets"
    data.mkdir(mode=0o700)
    secrets.mkdir(mode=0o700)
    return settings.model_copy(
        update={
            "database_url": f"sqlite:///{data / 'telegram_digest.db'}",
            "tg_session_path": data / "telegram_digest.session",
            "gmail_oauth_token_path": data / "gmail_oauth_token.json",
            "gmail_oauth_client_secret_path": secrets / "google_oauth_client.json",
        }
    )


def _create_runtime_files(settings, env_path: Path) -> None:
    for path in [
        env_path,
        settings.tg_session_path,
        settings.gmail_oauth_token_path,
        settings.gmail_oauth_client_secret_path,
    ]:
        path.write_text("secret-value", encoding="utf-8")
        path.chmod(0o600)


def test_security_check_detects_missing_files(settings, tmp_path: Path) -> None:
    deployment_settings = _deployment_paths(settings, tmp_path)

    errors = check_security(deployment_settings, env_path=tmp_path / ".env")

    assert ".env is missing." in errors
    assert "Telegram session is missing." in errors
    assert "Gmail OAuth token is missing." in errors
    assert "Gmail OAuth client JSON is missing." in errors


def test_security_check_detects_insecure_permissions(settings, tmp_path: Path) -> None:
    deployment_settings = _deployment_paths(settings, tmp_path)
    env_path = tmp_path / ".env"
    _create_runtime_files(deployment_settings, env_path)
    deployment_settings.tg_session_path.chmod(0o644)
    deployment_settings.gmail_oauth_client_secret_path.parent.chmod(0o755)

    errors = check_security(deployment_settings, env_path=env_path)

    assert "Telegram session must have mode 600." in errors
    assert "secrets directory must have mode 700." in errors


def test_healthcheck_does_not_print_secrets(settings, tmp_path: Path) -> None:
    deployment_settings = _deployment_paths(settings, tmp_path)
    env_path = tmp_path / ".env"
    _create_runtime_files(deployment_settings, env_path)
    init_db(deployment_settings)
    output = io.StringIO()

    assert run_healthcheck(deployment_settings, output=lambda line: output.write(f"{line}\n"))

    rendered = output.getvalue()
    assert "test-key" not in rendered
    assert "secret-value" not in rendered


def test_backup_script_exists_and_is_executable() -> None:
    script = Path("deploy/backup_sqlite.sh")

    assert script.is_file()
    assert stat.S_IMODE(script.stat().st_mode) & stat.S_IXUSR
