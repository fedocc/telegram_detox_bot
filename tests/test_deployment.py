from __future__ import annotations

import io
import os
import sqlite3
import stat
import subprocess
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


def test_backup_uses_runtime_sqlite_url_and_verifies_copy(tmp_path: Path) -> None:
    project_root = Path.cwd().resolve()
    runtime_root = tmp_path / "runtime"
    database = runtime_root / "state" / "custom.sqlite"
    database.parent.mkdir(parents=True)
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE durable (value TEXT NOT NULL)")
        connection.execute("INSERT INTO durable VALUES ('preserved')")

    (runtime_root / ".venv").symlink_to(project_root / ".venv", target_is_directory=True)
    environment = os.environ.copy()
    environment.update({
        "DATABASE_URL": "sqlite:///state/custom.sqlite",
        "PYTHONPATH": str(project_root),
    })
    result = subprocess.run(  # noqa: S603 - absolute reviewed script and temp-only arguments
        [str(project_root / "deploy/backup_sqlite.sh"), str(runtime_root)],
        cwd=runtime_root,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    backups = list((runtime_root / "backups").glob("*.sqlite"))
    assert len(backups) == 1
    assert stat.S_IMODE((runtime_root / "backups").stat().st_mode) == 0o700
    assert stat.S_IMODE(backups[0].stat().st_mode) == 0o600
    with sqlite3.connect(backups[0]) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute("SELECT value FROM durable").fetchone() == ("preserved",)


def test_backup_rejects_non_sqlite_database_url(tmp_path: Path) -> None:
    project_root = Path.cwd().resolve()
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    (runtime_root / ".venv").symlink_to(project_root / ".venv", target_is_directory=True)
    environment = os.environ.copy()
    environment.update({
        "DATABASE_URL": "postgresql://localhost/telegram",
        "PYTHONPATH": str(project_root),
    })

    result = subprocess.run(  # noqa: S603 - absolute reviewed script and temp-only arguments
        [str(project_root / "deploy/backup_sqlite.sh"), str(runtime_root)],
        cwd=runtime_root,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "file-backed SQLite" in result.stderr
    assert not (runtime_root / "backups").exists()


def test_aeza_deploy_backs_up_database_before_pull_and_restart() -> None:
    script = Path("tools/deploy_aeza.sh").read_text(encoding="utf-8")

    verified = script.index('git rev-parse origin/main)" = "$expected"')
    backup = script.index('git show "${expected}:deploy/backup_sqlite.sh"')
    pull = script.index("git pull --ff-only")
    installer = script.index("'pip>=26.2,<27'")
    dependencies = script.index("pip install --disable-pip-version-check -e .")
    restart = script.index("systemctl restart telegram-detox.service")
    assert verified < backup < pull < installer < dependencies < restart


def test_tailscale_setup_is_private_serve_only() -> None:
    script = Path("deploy/configure_tailscale_serve.sh")
    source = script.read_text(encoding="utf-8")

    assert stat.S_IMODE(script.stat().st_mode) & stat.S_IXUSR
    assert "tailscale serve --bg 8787" in source
    assert "tailscale funnel " not in source
    assert "http://127.0.0.1:8787" in source


def test_cloudflare_tunnel_keeps_origin_private_and_denies_unmatched_hosts() -> None:
    script = Path("deploy/configure_cloudflare_tunnel.sh")
    source = script.read_text(encoding="utf-8")

    assert stat.S_IMODE(script.stat().st_mode) & stat.S_IXUSR
    assert "http://127.0.0.1:8787" in source
    assert "http_status:404" in source
    assert "trycloudflare.com" in source
    assert "no-autoupdate: true" in source
    assert ".".join(["0"] * 4) not in source
    assert "tailscale funnel" not in source.lower()
    assert "systemctl is-active cloudflared" in source


def test_web_push_key_generator_keeps_private_key_in_mode_600_env(tmp_path: Path) -> None:
    project_root = Path.cwd().resolve()
    env_path = tmp_path / ".env"
    env_path.write_text("WEB_PUSH_VAPID_PRIVATE_KEY=\nUNCHANGED=yes\n", encoding="utf-8")
    env_path.chmod(0o644)

    result = subprocess.run(  # noqa: S603 - reviewed script writes only to the temp root
        [
            str(project_root / ".venv/bin/python"),
            str(project_root / "deploy/configure_web_push.py"),
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    lines = env_path.read_text(encoding="utf-8").splitlines()
    private_key = next(line.split("=", 1)[1] for line in lines
                       if line.startswith("WEB_PUSH_VAPID_PRIVATE_KEY="))
    assert private_key and private_key not in result.stdout and private_key not in result.stderr
    assert "Public VAPID key:" in result.stdout
    assert "UNCHANGED=yes" in lines
    assert stat.S_IMODE(env_path.stat().st_mode) == 0o600

    repeated = subprocess.run(  # noqa: S603 - reviewed script reads the same temp root
        [
            str(project_root / ".venv/bin/python"),
            str(project_root / "deploy/configure_web_push.py"),
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert repeated.returncode == 0
    assert private_key not in repeated.stdout and private_key not in repeated.stderr
    assert env_path.read_text(encoding="utf-8").splitlines() == lines


def test_healthcheck_requires_ai_key_only_in_legacy(settings, tmp_path):
    from app.cli.healthcheck import check_health

    deployment_settings = _deployment_paths(settings, tmp_path)
    _create_runtime_files(deployment_settings, tmp_path / ".env")
    init_db(deployment_settings)
    for mention_only in (True, False):
        configured = deployment_settings.model_copy(update={
            "mention_only_mode": mention_only, "aitunnel_api_key": "",
        })
        errors = check_health(configured)
        assert errors == ([] if mention_only else ["AITunnel API key is not configured."])
