# Upivo

Агрегатор публикаций Telegram, сайтов и RSS/Habr.

Стек: Python 3.12, uv, FastAPI, PostgreSQL, SQLAlchemy 2, Alembic.
Redis и Celery подготовлены; worker/beat запускаются опционально.

Ветка `deploy/local-demo` предназначена для отдельного публичного демостенда, а не рабочего окружения разработки. Настройка и запуск: **[локальный демостенд](docs/LOCAL_DEMO.md)**. Автоматический запуск через `start.py` не заменяет настройку секретов.

## Запуск

Нужен Docker с Compose. Сначала создайте `.env` по [инструкции демостенда](docs/LOCAL_DEMO.md): задайте `POSTGRES_PASSWORD`, `AUTH_TOKEN_SECRET` и нужные ключи. Значения секретов не храните в Git.

```bash
python3 start_prod.py
```

Скрипт запускает Docker Desktop, оба Compose-проекта и управляемый ngrok, дожидается готовности и печатает ссылки; существующий .env не меняет.

Миграции применяются автоматически. Swagger через шлюз: http://127.0.0.1:18080/upivo/docs · Health API напрямую: http://127.0.0.1:18000/health. Инструкция: [локальный демостенд](docs/LOCAL_DEMO.md).

Остановка: `docker compose --profile workers down` — данные БД сохраняются.

## Проверки

Нужен uv. Из корня проекта:

```bash
cd backend
uv sync --locked
uv run pytest
uv run ruff check .
```

## Документация

- [Установка и локальный запуск](docs/SETUP.md)
- [Разработка и миграции](docs/DEVELOPMENT.md)
- [Архитектура](docs/ARCHITECTURE.md)
- [Решение проблем](docs/TROUBLESHOOTING.md)
