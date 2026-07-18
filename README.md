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

- Private messages are never silently dropped. After every LLM digest, the app checks all incoming private message IDs for the day. Any private message omitted by the LLM is added to `REVIEW`.
- Private messages are never counted as P3/background noise. If classification is uncertain, the message is surfaced for review.
- Immediate email is reserved for `P0_STRICT`, with recall prioritized over precision. In private chats, requests, planning or availability questions, important context, urgency, and borderline messages where a response may be expected qualify; obvious small talk remains digest-only. In groups, exact configured username mentions, replies, urgency, importance, deadlines, actionable requests, questions, and watchlist matches qualify. Set exact usernames with `P0_MENTION_USERNAMES`.
- Trusted private senders can lower uncertainty, but ordinary messages such as `привет` remain digest-only.
- `P0_CANDIDATE` and `NOT_P0` stay in the digest.
- Every `P0_STRICT` email is in Russian and includes chat title, sender, timestamp, a local classification reason, suggested action, a deadline derived from the message text, complete original text, and up to ten previous messages from the same chat. Model-generated English comments are not rendered.
- Every non-P0 conversation gets a concise semantic digest summary. Quiet chats are summarized instead of being reduced to message counts whenever text is available.
- AITunnel/LLM outage triggers a deterministic fallback digest. The fallback includes incoming private messages, group counts, P0 review candidates, and unprocessed media notices.
- Runtime never performs Telegram login. `python -m app.cli.telegram_login` is the only interactive authentication command. The 24/7 listener only connects with an existing session and exits closed if the session is missing or unauthorized.
- The service is read-only by design. Static tests fail if runtime code uses Telegram write/action methods such as send, delete, reaction, pin, mute, join, leave, or mark-read calls.

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

- `EMAIL_FROM`
- `EMAIL_TO`
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
7. Complete Google login under the same account as `EMAIL_FROM`.
8. Run `python -m app.cli.test_email`.

OAuth scope is only `https://www.googleapis.com/auth/gmail.send`. With this minimum scope,
`gmail_auth` cannot read the Gmail profile to verify the account. `test_email` is the
real verification: it confirms the OAuth account can send as `EMAIL_FROM`.

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
python -m app.cli.test_email
python -m app.cli.telegram_login
python -m app.cli.run
python -m app.cli.digest_now
python -m app.cli.cleanup
python -m app.cli.gmail_auth
python -m app.cli.check_birthdays --dry-run
python -m app.cli.check_birthdays
```

## Docker

```bash
docker compose build
docker compose run --rm telegram-digest python -m app.cli.telegram_login
docker compose up -d
```

`./data` и `./logs` монтируются как volume. `.env`, runtime-файлы в `data/`, `logs/`, `*.session` и базы исключены из Git; исключение — безопасный `data/birthdays.example.json`.

## Будущий перенос на VPS

1. Установите Docker и Docker Compose.
2. Скопируйте код без `.env`, `data/`, `logs/`.
3. Создайте `.env` на VPS через `python -m app.cli.setup_env` или безопасно перенесите локальный `.env`.
4. Перенесите `data/telegram_digest.session` только по SSH/SCP на доверенный сервер.
5. Для Gmail API перенесите `secrets/google_oauth_client.json` и `data/gmail_oauth_token.json` только по защищённому каналу. Это секреты, их нельзя класть в Git.
6. Выполните `chmod 600 data/telegram_digest.session .env secrets/google_oauth_client.json data/gmail_oauth_token.json`.
7. На VPS сервис использует refresh token и не открывает browser.
8. Запустите `docker compose up -d`.

## Как остановить сервис

Локально: `Ctrl+C`.

Docker:

```bash
docker compose down
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
- `.env`, session-файлы, `data/birthdays.json`, остальные runtime-файлы в `data/`, logs, secrets и database files не должны попадать в Git/GitHub.

## Тесты без Telegram и сети

Unit tests используют искусственные fixtures и fake LLM/email-клиенты. Реальные API keys, сеть и Telegram session не требуются.
