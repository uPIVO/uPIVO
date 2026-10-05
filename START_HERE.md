# Окружение Upivo для разработки

Это стартовый каркас проекта. В нём подготовлены FastAPI, PostgreSQL, Redis,
SQLAlchemy, Alembic, Celery, библиотеки для Telegram/RSS/сайтов, pytest и ruff.
Продуктовую логику команда пишет в `backend/app/`. Frontend пока не реализован.

## Первый запуск

Установи uv и Docker с Compose либо Podman.
Ссылки и установка uv: [docs/SETUP.md](docs/SETUP.md).
Скачай проект удобным способом и открой терминал в его корневой папке.

```bash
uv run --no-project --python 3.12 start.py
```

Команда одинакова для Linux, macOS и Windows. uv подберёт или скачает Python 3.12.

Если Python уже установлен, можно запускать `python3 start.py` на Linux/macOS
или `py start.py` на Windows. Сам файл запуска требует Python 3.10+;
backend всегда использует Python 3.12.

`start.py` создаёт `.env` из `.env.example`, устанавливает закреплённые зависимости
в `backend/.venv`, запускает PostgreSQL и Redis, ждёт их готовности, применяет
миграции и запускает API с автоматической перезагрузкой при изменении кода.
Для Podman локальный Compose устанавливается автоматически в `.tools/compose`.
Существующий `.env` сохраняется. Базу и Redis нужно оставить запущенными во время работы.

Открой http://localhost:8000/docs. Если менял `APP_PORT` в `.env`, используй свой порт.
`GET /health` проверяет API, `GET /health/db` — подключение к базе.

## Ежедневные команды

Все команды выполняются из корня репозитория:

```bash
uv run --no-project --python 3.12 start.py              # подготовить и запустить
uv run --no-project --python 3.12 start.py --setup      # только подготовить окружение
uv run --no-project --python 3.12 start.py --check      # ruff и pytest
uv run --no-project --python 3.12 start.py --stop       # остановить БД и Redis
```

API останавливается через Ctrl+C. Затем выполни `--stop`, если БД и Redis больше
не нужны. Данные БД сохраняются в volume. Следующий обычный запуск их использует.
Проверки `--check` не требуют работающих контейнеров.

Для работы только над HTTP-маршрутами можно запускать `start.py --api-only`:
он установит зависимости и запустит API, пропустив контейнеры и миграции.
Для функций с базой нужен полный запуск.

## Где писать проект

| Задача | Папка |
| --- | --- |
| HTTP-маршруты | `backend/app/api/`; подключи router в `app/main.py` |
| Форматы запросов и ответов | `backend/app/schemas/` |
| Логика продукта | `backend/app/services/` |
| Модели таблиц | `backend/app/models/` + новая миграция Alembic |
| Чтение Telegram, RSS, сайтов | `backend/app/connectors/` |
| Обработка текста | `backend/app/processing/` |
| Фоновые задачи | `backend/app/workers/` |
| Тесты | `backend/tests/` |
| Будущий frontend | `frontend/` |

Начни с маленького изменения, проверь его в Swagger, добавь тест и выполни `--check`.

`backend/sql/upivo_schema.sql` — проект будущей схемы; стартовая миграция пока
пустая. SQL-файл не применяется при запуске. Новые таблицы добавляй через ORM-модели
и Alembic по [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md).
Celery worker/beat включаются отдельно по инструкции:
[docs/SETUP.md](docs/SETUP.md#опциональные-workers).

## Конфигурация и пакеты

`.env`, ключи, Telegram-сессии, `.venv` и `.tools` — локальные файлы окружения.
Ключи Telegram и LLM для запуска окружения не нужны; они понадобятся при реализации
соответствующих модулей.

Если порт занят, измени `POSTGRES_PORT` / `REDIS_PORT` / `APP_PORT` в `.env`.
Для БД и Redis также обнови порт в `DATABASE_URL` / `REDIS_URL`.
Остальные решения: [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md).

Пакеты backend уже перечислены в `backend/pyproject.toml` и `backend/uv.lock`.
Новую зависимость добавляй из `backend/` командой `uv add имя-пакета`;
для инструмента разработки — `uv add --dev имя-пакета`.
uv обновляет `pyproject.toml` и `uv.lock` вместе. Для форматирования из `backend/`:
`uv run ruff format --config pyproject.toml . ../start.py`.
