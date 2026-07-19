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
cd /opt/telegram-detox
sudo -u telegram-detox python3.12 -m venv .venv
sudo -u telegram-detox .venv/bin/python -m pip install --upgrade pip
sudo -u telegram-detox .venv/bin/python -m pip install -e '.[dev]'
sudo -u telegram-detox cp .env.example .env
sudo -u telegram-detox install -d -m 700 data secrets
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
cd /opt/telegram-detox
sudo chown -R telegram-detox:telegram-detox data secrets .env
sudo chmod 600 .env
sudo chmod 600 data/*.session
sudo chmod 600 data/gmail_oauth_token.json
sudo chmod 600 secrets/google_oauth_client.json
sudo chmod 700 data secrets
sudo -u telegram-detox .venv/bin/python -m app.cli.security_check
sudo -u telegram-detox .venv/bin/python -m app.cli.healthcheck
sudo -u telegram-detox .venv/bin/python -m app.cli.cancel_legacy_alerts
```

The OAuth client JSON and token are secrets. After copying them to a VPS, keep mode
`600`. The VPS refreshes the OAuth token; it does not open a browser.

### Change to the dedicated Gmail sender

Keep the existing main Gmail recipient. Do not print the environment file or either OAuth
JSON file while completing these steps.

1. Stop the service:

   ```bash
   sudo systemctl stop telegram-detox
   ```

2. In `/opt/telegram-detox/.env`, set `GMAIL_SENDER_EMAIL=fnikonov999@gmail.com`,
   `GMAIL_SENDER_NAME=TELEGRAM`, and set `GMAIL_RECIPIENT_EMAIL` to the existing recipient
   value.

3. Back up the old token without displaying it:

   ```bash
   cd /opt/telegram-detox
   sudo -u telegram-detox bash -c '
     backup_path=$(mktemp "data/gmail_oauth_token.json.bak.$(date -u +%Y%m%d_%H%M%S).XXXXXX") &&
     install -m 600 data/gmail_oauth_token.json "$backup_path"
   '
   ```

   `mktemp` creates an exclusive filename, so repeated migrations cannot overwrite an earlier
   backup. The auth CLI creates another unique backup before replacing the token and aborts if
   either the backup or replacement token cannot be verified as mode `600`.

4. Run Gmail OAuth on an interactive machine where the browser callback is reachable:

   ```bash
   .venv/bin/python -m app.cli.gmail_auth
   ```

   Log in as `fnikonov999@gmail.com`. If authentication is performed on another machine,
   transfer only the resulting token through a secure channel, preserve the production backup,
   and set the production token owner to `telegram-detox` with mode `600`.

5. On the VPS, verify the accounts and send the test message:

   ```bash
   sudo -u telegram-detox .venv/bin/python -m app.cli.test_email --account-check
   sudo -u telegram-detox .venv/bin/python -m app.cli.test_email
   ```

   Require `can_send=true`, verify the recipient is unchanged, and confirm receipt of the
   `[Telegram Detox][Test] Gmail sender check` message.

6. Restart the service:

   ```bash
   sudo systemctl start telegram-detox
   sudo systemctl status telegram-detox --no-pager -l
   ```

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

## 5. Safe existing-server update

Push the verified local branch before the VPS pulls it.

On Mac:

```bash
cd ~/Projects/telegram-digest
pytest
ruff check .
git status
git push
```

On VPS, connect as root or a user that can run `runuser` and manage systemd:

```bash
ssh <VPS_HOST>
cd /opt/telegram-detox
systemctl stop telegram-detox || true
runuser -u telegram-detox -- git pull
runuser -u telegram-detox -- .venv/bin/python -m pip install -e .
runuser -u telegram-detox -- .venv/bin/python -m pytest
runuser -u telegram-detox -- .venv/bin/python -m ruff check .
runuser -u telegram-detox -- .venv/bin/python -m app.cli.healthcheck
runuser -u telegram-detox -- .venv/bin/python -m app.cli.security_check
runuser -u telegram-detox -- .venv/bin/python -m app.cli.cancel_legacy_alerts
sqlite3 data/telegram_digest.db "SELECT status, alert_type AS kind, COUNT(*) FROM alert_jobs GROUP BY status, alert_type ORDER BY status, alert_type;"
systemctl start telegram-detox
systemctl status telegram-detox --no-pager -l
journalctl -u telegram-detox -n 120 --no-pager
```

`alert_type AS kind` keeps the queue inspection output stable while using the actual
database column name.

## Backup

Run the local SQLite backup as the service user. Backups stay in ignored `backups/`.

```bash
sudo -u telegram-detox /opt/telegram-detox/deploy/backup_sqlite.sh
```
