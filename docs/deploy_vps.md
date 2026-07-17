# Ubuntu VPS deployment

Run these commands from a local terminal. Do not put `.env`, the Telegram session,
or Gmail OAuth files in Git.

## 1. Create the service user and install packages

```bash
sudo useradd --system --create-home --home-dir /opt/telegram-detox --shell /bin/bash telegram-detox
sudo apt update
sudo apt install -y git python3.12 python3.12-venv sqlite3
sudo install -d -o telegram-detox -g telegram-detox -m 700 /opt/telegram-detox
```

## 2. Install the application

```bash
sudo -u telegram-detox git clone <PRIVATE_REPOSITORY_URL> /opt/telegram-detox
sudo -u telegram-detox python3.12 -m venv /opt/telegram-detox/.venv
sudo -u telegram-detox /opt/telegram-detox/.venv/bin/python -m pip install --upgrade pip
sudo -u telegram-detox /opt/telegram-detox/.venv/bin/python -m pip install -e '/opt/telegram-detox[dev]'
sudo -u telegram-detox cp /opt/telegram-detox/.env.example /opt/telegram-detox/.env
sudo -u telegram-detox install -d -m 700 /opt/telegram-detox/data /opt/telegram-detox/secrets
```

Edit `/opt/telegram-detox/.env` manually. Keep it out of shell history and Git.

## 3. Transfer runtime secrets securely

Transfer these files only through a secure channel, then set ownership and permissions:

```text
/opt/telegram-detox/.env
/opt/telegram-detox/data/telegram_digest.session
/opt/telegram-detox/secrets/google_oauth_client.json
/opt/telegram-detox/data/gmail_oauth_token.json
```

```bash
sudo chown -R telegram-detox:telegram-detox /opt/telegram-detox/data /opt/telegram-detox/secrets /opt/telegram-detox/.env
sudo chmod 600 /opt/telegram-detox/.env
sudo chmod 600 /opt/telegram-detox/data/*.session
sudo chmod 600 /opt/telegram-detox/data/gmail_oauth_token.json
sudo chmod 600 /opt/telegram-detox/secrets/google_oauth_client.json
sudo chmod 700 /opt/telegram-detox/data /opt/telegram-detox/secrets
sudo -u telegram-detox /opt/telegram-detox/.venv/bin/python -m app.cli.security_check
sudo -u telegram-detox /opt/telegram-detox/.venv/bin/python -m app.cli.healthcheck
sudo -u telegram-detox /opt/telegram-detox/.venv/bin/python -m app.cli.cancel_legacy_alerts
```

The OAuth client JSON and token are secrets. After copying them to a VPS, keep mode
`600`. The VPS refreshes the OAuth token; it does not open a browser.

## 4. Install and operate systemd

```bash
sudo cp /opt/telegram-detox/deploy/telegram-detox.service.example /etc/systemd/system/telegram-detox.service
sudo systemctl daemon-reload
sudo systemctl enable --now telegram-detox
sudo systemctl status telegram-detox
sudo systemctl stop telegram-detox
sudo systemctl start telegram-detox
sudo systemctl restart telegram-detox
sudo journalctl -u telegram-detox -f
```

The unit logs to `journalctl`; it contains no secrets.

## 5. Update

```bash
sudo -u telegram-detox git -C /opt/telegram-detox pull
sudo -u telegram-detox /opt/telegram-detox/.venv/bin/python -m app.cli.cleanup
sudo -u telegram-detox /opt/telegram-detox/.venv/bin/python -m pytest
sudo -u telegram-detox /opt/telegram-detox/.venv/bin/python -m ruff check .
sudo systemctl restart telegram-detox
```

## Backup

Run the local SQLite backup as the service user. Backups stay in ignored `backups/`.

```bash
sudo -u telegram-detox /opt/telegram-detox/deploy/backup_sqlite.sh
```
