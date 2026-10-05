# Upivo

Агрегатор публикаций Telegram, сайтов и RSS/Habr.

Стек: Python 3.12, uv, FastAPI, PostgreSQL, SQLAlchemy 2, Alembic.
Redis и Celery подготовлены; worker/beat запускаются опционально.

Новичкам: **[Начни здесь — готовое окружение для разработки](START_HERE.md)**.
Из корня на любой системе: `uv run --no-project --python 3.12 start.py`.

## Запуск

Нужен Docker с Compose. Из корня проекта:

```bash
cp .env.example .env
docker compose up --build -d --wait
```

В PowerShell: `Copy-Item .env.example .env` вместо `cp`.
Миграции применяются автоматически.

Swagger: http://localhost:8000/docs · Health: http://localhost:8000/health

Остановка: `docker compose down` — данные БД сохраняются.

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
