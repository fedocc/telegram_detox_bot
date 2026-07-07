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
- P0 is fail-open for personal messages. If the lightweight P0 classifier fails for an incoming private message, the app sends an immediate `[ПРОВЕРЬ] новое личное сообщение` email.
- Group `REVIEW` may wait until the evening digest unless the local urgency prefilter matched obvious urgency. Obvious group urgency plus LLM failure sends an immediate fallback email.
- AITunnel/LLM outage triggers a deterministic fallback digest. The fallback includes incoming private messages, group counts, P0 review candidates, and unprocessed media notices.
- Runtime never performs Telegram login. `python -m app.cli.telegram_login` is the only interactive authentication command. The 24/7 listener only connects with an existing session and exits closed if the session is missing or unauthorized.
- The service is read-only by design. Static tests fail if runtime code uses Telegram write/action methods such as send, delete, reaction, pin, mute, join, leave, or mark-read calls.

## Риск MTProto session

Telethon session-файл фактически даёт доступ к аккаунту в рамках созданной сессии. Не кладите `*.session` в GitHub, не пересылайте его, не храните на публичном сервере и не передавайте третьим лицам. Для VPS переносите session только по защищённому каналу, выставляйте права `600`, а при компрометации завершайте сессию в Telegram settings.

## Локальный запуск

```bash
cd ~/Projects/telegram-digest
make setup
source .venv/bin/activate
python -m app.cli.setup_env
python -m app.cli.test_llm
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

## Gmail App Password

Для V1 используется Gmail SMTP через App Password:

- `SMTP_HOST=smtp.gmail.com`
- `SMTP_PORT=465`
- `SMTP_USERNAME`
- `SMTP_PASSWORD`
- `EMAIL_FROM`
- `EMAIL_TO`

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

LLM-ответы валидируются Pydantic-моделями. При невалидном JSON выполняется один repair retry. Бесконечных повторов нет. Если P0 candidate не удалось классифицировать из-за LLM/API ошибки, отправляется fallback email `[ВОЗМОЖНО СРОЧНО]`.

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
```

## Docker

```bash
docker compose build
docker compose run --rm telegram-digest python -m app.cli.telegram_login
docker compose up -d
```

`./data` и `./logs` монтируются как volume. `.env`, `data/`, `logs/`, `*.session` и базы исключены из Git.

## Будущий перенос на VPS

1. Установите Docker и Docker Compose.
2. Скопируйте код без `.env`, `data/`, `logs/`.
3. Создайте `.env` на VPS через `python -m app.cli.setup_env` или безопасно перенесите локальный `.env`.
4. Перенесите `data/telegram_digest.session` только по SSH/SCP на доверенный сервер.
5. Выполните `chmod 600 data/telegram_digest.session .env`.
6. Запустите `docker compose up -d`.

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
- Тексты сообщений, API keys, SMTP credentials и session details не пишутся намеренно в логи; logging filter редактирует секретоподобные значения.
- `.env`, session-файлы, data, logs и database files не должны попадать в Git/GitHub.

## Тесты без Telegram и сети

Unit tests используют искусственные fixtures и fake LLM/email-клиенты. Реальные API keys, сеть и Telegram session не требуются.
