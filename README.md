# telegram-digest

MVP-сервис, который читает новые сообщения Telegram через личный MTProto-аккаунт Telethon, сохраняет контекст локально, отправляет немедленные email для P0-событий и один вечерний HTML digest в 20:30 Europe/Moscow.

Codex не является частью runtime-пайплайна. В runtime LLM-вызовы идут через Claude Haiku 4.5 в AITunnel с OpenAI Python SDK.

## Границы V1

- Используется Telethon / MTProto под личным аккаунтом.
- Telegram Bot API и Chat Automation не используются.
- Secret Chats не поддерживаются: Telegram не отдаёт их через обычный MTProto client session.
- Сервис read-only: не отправляет сообщения, не удаляет сообщения, не ставит реакции, не меняет настройки, не вызывает mark-read намеренно и не выполняет действий от имени аккаунта.
- Анализируются только текст, подписи к медиа и metadata о типе медиа.
- Фото, voice, видео, документы, стикеры не скачиваются и не отправляются в LLM.

## Safety guarantees and limits

- Private messages are never silently dropped unless their exact chat ID is configured in the explicit ignored-chat blacklist. After every LLM digest, the app checks all other incoming private message IDs for the day. Any private message omitted by the LLM is added to `REVIEW`.
- Private messages are never counted as P3/background noise. If classification is uncertain, the message is surfaced for review.
- Immediate email is reserved for `P0_STRICT`, with recall prioritized over precision. In private chats, requests, planning or availability questions, important context, urgency, and exact configured username mentions qualify; obvious small talk remains digest-only. In groups, exact mentions, replies, urgency, importance, deadlines, actionable requests, questions, and watchlist matches qualify. Channels are digest-only unless a post or media caption contains an exact configured mention. Set comma-separated exact usernames with `P0_MENTION_USERNAMES`.
- Trusted private senders can lower uncertainty, but ordinary messages such as `привет` remain digest-only.
- `P0_CANDIDATE` and `NOT_P0` stay in the digest.
- Every `P0_STRICT` email is in Russian and includes chat title, sender, timestamp, a local classification reason, suggested action, a deadline derived from the message text, complete original text, and up to ten previous messages from the same chat. Model-generated English comments are not rendered.
- Every non-P0 conversation gets a concise semantic digest summary. Quiet chats are summarized instead of being reduced to message counts whenever text is available.
- AITunnel/LLM outage triggers a deterministic fallback digest. The fallback includes incoming private messages, group counts, P0 review candidates, and unprocessed media notices.
- Runtime never performs Telegram login. `python -m app.cli.telegram_login` is the only interactive authentication command. The 24/7 listener only connects with an existing session and exits closed if the session is missing or unauthorized.
- The service is read-only by design. Static tests fail if runtime code uses Telegram write/action methods such as send, delete, reaction, pin, mute, join, leave, or mark-read calls.

## How to make sure I see your message

Если важно — тегните @fedocc.

Точное упоминание работает в личных чатах, группах, каналах и подписях к медиа. Регистр не
важен, но username должен совпадать полностью. Например, `@fedocc_bot` не совпадает с
`@fedocc`. По умолчанию используются:

```env
P0_MENTION_USERNAMES=fedocc,me,fedornikonov
```

Обычные сообщения каналов попадают в digest, но не создают немедленный P0. Исключение —
точное упоминание настроенного username. Игнорируемые чаты не сохраняются и не попадают ни
в P0, ни в digest.

Безопасная локальная проверка не обращается к Telegram или БД и не печатает исходный текст:

```bash
python -m app.cli.p0_check --chat-type private --text "@fedocc привет"
python -m app.cli.p0_check --chat-type group --text "@fedocc привет"
python -m app.cli.p0_check --chat-type channel --text "Опубликовано распределение студентов"
python -m app.cli.p0_check --chat-type channel --text "@fedocc посмотри"
```

## Игнорируемые чаты

Чаты из blacklist отбрасываются по точному `chat_id` до чтения текста, записи в БД, backfill, P0 и LLM/digest. Название чата не используется как идентификатор.

ID можно задать через env:

```env
IGNORE_CHAT_IDS=-1001234567890
```

Локальный файл `data/ignored_chats.json` имеет тот же формат, что `data/ignored_chats.example.json`. Локальный файл игнорируется Git; env и JSON объединяются без дублей.

Проверить конфигурацию и найти точные ID можно read-only командами:

```bash
python -m app.cli.check_ignored_chats
python -m app.cli.list_chats --limit 100
python -m app.cli.list_chats --search "name"
```

## Напоминания о днях рождения

Отдельный birthday-модуль читает `contacts.getBirthdays` через raw Telethon API каждые 6 часов и сохраняет локальный cache контактов. Он не проходит через P0 classifier и не вызывает Telegram write API. Ежедневное русскоязычное письмо отправляется в настроенное время; если день рождения на сегодня обнаружен позже, пропущенное уведомление отправляется один раз сразу.

Настройки:

```env
BIRTHDAY_REMINDERS_ENABLED=true
BIRTHDAY_POLL_INTERVAL_HOURS=6
BIRTHDAY_REMINDER_TIME=09:00
BIRTHDAY_LOOKAHEAD_DAYS=1
BIRTHDAY_MANUAL_PATH=data/birthdays.json
```

Для manual fallback скопируйте `data/birthdays.example.json` в `data/birthdays.json` и измените локальную копию. `data/birthdays.json` игнорируется Git. Telegram и manual-записи с одинаковым username или именем и датой объединяются без повторного email.

Проверка без отправки письма не выводит реальные имена и даты:

```bash
python -m app.cli.check_birthdays --dry-run
python -m app.cli.check_birthdays
```

## Риск MTProto session

Telethon session-файл фактически даёт доступ к аккаунту в рамках созданной сессии. Не кладите `*.session` в GitHub, не пересылайте его, не храните на публичном сервере и не передавайте третьим лицам. Для VPS переносите session только по защищённому каналу, выставляйте права `600`, а при компрометации завершайте сессию в Telegram settings.

## Локальный запуск

```bash
cd ~/Projects/telegram-digest
make setup
source .venv/bin/activate
python -m app.cli.setup_env
python -m app.cli.test_llm
python -m app.cli.gmail_auth
python -m app.cli.test_email --account-check
python -m app.cli.test_email
python -m app.cli.telegram_login
python -m app.cli.run
```

`setup_env` создаёт `.env` с правами `600`, секреты вводятся hidden prompt и не печатаются обратно.

## Telegram API credentials

Создайте `api_id` и `api_hash` на `https://my.telegram.org`. Запишите их через:

```bash
python -m app.cli.setup_env
```

Затем выполните:

```bash
python -m app.cli.telegram_login
```

Команда спросит Telegram code интерактивно и 2FA password через hidden prompt, если 2FA включена. Listener после login не запускается автоматически.

## Gmail API setup

Default email transport is Gmail API over HTTPS:

- `GMAIL_SENDER_EMAIL=fnikonov999@gmail.com`
- `GMAIL_RECIPIENT_EMAIL=<current main Gmail recipient>`
- `EMAIL_TRANSPORT=gmail_api`
- `GMAIL_OAUTH_CLIENT_SECRET_PATH=secrets/google_oauth_client.json`
- `GMAIL_OAUTH_TOKEN_PATH=data/gmail_oauth_token.json`

Setup:

1. Create a Google Cloud project.
2. Enable Gmail API.
3. Create OAuth client: Desktop app.
4. Download OAuth client JSON.
5. Save it as `secrets/google_oauth_client.json`.
6. Run `python -m app.cli.gmail_auth`.
7. When prompted with `Log in as the sender account.`, authenticate as the account in
   `GMAIL_SENDER_EMAIL`.
8. Run `python -m app.cli.test_email --account-check` and require `can_send=true`.
9. Run `python -m app.cli.test_email`.

OAuth uses Gmail send permission plus Google email identity permission. The identity check
compares the authenticated account with `GMAIL_SENDER_EMAIL`; Gmail aliases are not used.
For compatibility, an existing `EMAIL_TO` is used only when `GMAIL_RECIPIENT_EMAIL` is empty,
so the current recipient remains unchanged during migration. New configurations should set
`GMAIL_RECIPIENT_EMAIL` explicitly. `EMAIL_FROM` and `EMAIL_TO` remain the SMTP-only fields.

### Switch the Gmail sender account on systemd

This production procedure applies to the systemd deployment. Do not display or commit the
environment file, OAuth client, token, or token backups.

1. Stop `telegram-detox.service`.
2. Set `GMAIL_SENDER_EMAIL=fnikonov999@gmail.com`. Copy the existing recipient value into
   `GMAIL_RECIPIENT_EMAIL`; do not change that address.
3. Create a unique mode-`600` backup as the service user:

   ```bash
   sudo -u telegram-detox bash -c '
     backup_path=$(mktemp "data/gmail_oauth_token.json.bak.$(date -u +%Y%m%d_%H%M%S).XXXXXX") &&
     install -m 600 data/gmail_oauth_token.json "$backup_path"
   '
   ```

   The auth CLI also creates a unique backup before replacement. Any chmod or final mode-check
   failure aborts authentication.
4. Run `.venv/bin/python -m app.cli.gmail_auth` and log in as the sender account.
5. Run `.venv/bin/python -m app.cli.test_email --account-check`. Continue only when it reports
   the dedicated authenticated sender, the unchanged recipient, and `can_send=true`.
6. Run `.venv/bin/python -m app.cli.test_email` and confirm receipt of
   `[Telegram Detox][Test] Gmail sender check`.
7. Restart `telegram-detox.service`.

## SMTP legacy mode

SMTP is optional legacy transport. Set `EMAIL_TRANSPORT=smtp` and configure:

- `SMTP_HOST=smtp.gmail.com`
- `SMTP_PORT=465` with `SMTP_TLS_MODE=ssl`, or `SMTP_PORT=587` with `SMTP_TLS_MODE=starttls`
- `SMTP_USERNAME`
- `SMTP_PASSWORD`

Проверка:

```bash
python -m app.cli.test_email
```

## AITunnel key

Используется:

```env
AITUNNEL_BASE_URL=https://api.aitunnel.ru/v1/
AITUNNEL_MODEL=claude-haiku-4.5
AITUNNEL_API_KEY=
```

Проверка:

```bash
python -m app.cli.test_llm
```

LLM-ответы валидируются Pydantic-моделями. При невалидном JSON выполняется один repair retry. Бесконечных повторов нет. Если P0 candidate не удалось классифицировать из-за LLM/API ошибки, сообщение остаётся `P0_CANDIDATE` до digest; немедленный fallback email не отправляется.

## Команды

```bash
make test
make lint
make test-llm
make test-email
make telegram-login
make run
make digest-now
make cleanup
```

Эквивалентные CLI:

```bash
python -m app.cli.test_llm
python -m app.cli.test_email --account-check
python -m app.cli.test_email
python -m app.cli.telegram_login
python -m app.cli.run
python -m app.cli.digest_now
python -m app.cli.cleanup
python -m app.cli.gmail_auth
python -m app.cli.check_birthdays --dry-run
python -m app.cli.check_birthdays
```

## Deploy на VPS через systemd

Этот VPS запускает приложение как `telegram-detox.service` из `/opt/telegram-detox` от пользователя `telegram-detox`. Docker для этого deployment не используется. Перед перезапуском cleanup-команды отменяют небезопасные legacy alert/digest jobs; `cancel_unsafe_digests` безопасно запускать повторно и выводит только счётчики.

Запускайте deployment от `root`:

```bash
cd /opt/telegram-detox
systemctl stop telegram-detox || true
runuser -u telegram-detox -- git pull
runuser -u telegram-detox -- .venv/bin/python -m pip install -e .
runuser -u telegram-detox -- .venv/bin/python -m pytest
runuser -u telegram-detox -- .venv/bin/python -m ruff check .
runuser -u telegram-detox -- .venv/bin/python -m app.cli.healthcheck
runuser -u telegram-detox -- .venv/bin/python -m app.cli.security_check
runuser -u telegram-detox -- .venv/bin/python -m app.cli.cancel_legacy_alerts
runuser -u telegram-detox -- .venv/bin/python -m app.cli.cancel_unsafe_digests
runuser -u telegram-detox -- .venv/bin/python -m app.cli.check_ignored_chats
runuser -u telegram-detox -- .venv/bin/python -m app.cli.check_birthdays --dry-run
systemctl start telegram-detox
systemctl enable telegram-detox
systemctl status telegram-detox --no-pager -l
```

`.env`, Telegram session, Gmail OAuth token, локальная БД и приватные JSON-файлы остаются локальными runtime-файлами и не должны попадать в Git. На VPS Gmail использует refresh token и не открывает browser.

## Как остановить сервис

Локально: `Ctrl+C`. На VPS:

```bash
systemctl stop telegram-detox
```

## Как полностью удалить session и локальную БД

Сначала остановите сервис, затем удалите локальные runtime-файлы:

```bash
rm -f data/*.session data/*.session-journal data/*.db data/*.db-journal
rm -rf logs/*
```

Также завершите соответствующую сессию в Telegram settings, если больше не планируете использовать этот сервис.

## Хранение и приватность

- SQLite по умолчанию: `sqlite:///data/telegram_digest.db`.
- Postgres поддерживается через `DATABASE_URL`.
- Raw messages удаляются через 14 дней.
- Digests хранятся 90 дней.
- Тексты сообщений, API keys, SMTP credentials, OAuth tokens, OAuth client secrets и session details не пишутся намеренно в логи; logging filter редактирует секретоподобные значения.
- `.env`, session-файлы, `data/birthdays.json`, `data/ignored_chats.json`, остальные runtime-файлы в `data/`, logs, secrets и database files не должны попадать в Git/GitHub.

## Тесты без Telegram и сети

Unit tests используют искусственные fixtures и fake LLM/email-клиенты. Реальные API keys, сеть и Telegram session не требуются.
