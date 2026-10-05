# Первый запуск проекта с нуля

Для новичков есть [START_HERE.md](../START_HERE.md) и запуск одной командой:
`uv run --no-project --python 3.12 start.py`. Он поддерживает Docker/Compose и Podman,
создаёт `.env`, устанавливает зависимости, запускает БД/Redis и применяет миграции.
При Podman Compose устанавливается локально в `.tools/compose`.
Нужен uv; Python 3.12 он подберёт или скачает сам. При прямом запуске через
`python3 start.py` / `py start.py` скрипт требует Python 3.10+.
Ручные способы установки и запуска приведены ниже.

Нужны Docker Engine с Compose v2+ или Docker Desktop. Для локального Python
и тестов нужен uv 0.12.22 и Python 3.12; uv может скачать Python сам.
Python 3.13 пока не поддерживается. PostgreSQL 16 и Redis 7 берутся из Compose.
Скачайте проект удобным способом. Команды ниже выполняются из его корневой папки.

Установка uv: https://docs.astral.sh/uv/getting-started/installation/
Linux/macOS (явно запускается пользователем):

```bash
curl -LsSf https://astral.sh/uv/0.12.22/install.sh | sh
```

Windows PowerShell:

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/0.12.22/install.ps1 | iex"
```

Откройте новый терминал и проверьте `uv --version`, `docker compose version`.
Bootstrap ничего системного не устанавливает.

## A. Docker — рекомендуемый вариант

```bash
cp .env.example .env
docker compose config --quiet
docker compose up --build -d --wait
docker compose ps
curl http://localhost:8000/health
curl http://localhost:8000/health/db
```

В PowerShell: `Copy-Item .env.example .env`, для HTTP используйте
`Invoke-RestMethod http://localhost:8000/health`.
Swagger откройте в браузере: http://localhost:8000/docs.
One-shot сервис migrate автоматически выполняет `alembic upgrade head` до API.
Повторное применение вручную (из корня):

```bash
docker compose run --rm migrate
```

Тесты и ruff (dev-зависимости не входят в runtime Docker image):

```bash
cd backend
uv sync --locked
uv run pytest
uv run ruff check .
cd ..
docker compose down
```

`down` сохраняет named volume PostgreSQL. Не используйте `down -v`, если нужны данные.
Изменения кода попадут в контейнер после `docker compose up --build -d --wait`.

## B. Локальный Python + uv

В корне:

```bash
cp .env.example .env
docker compose up -d --wait postgres redis
cd backend
uv python install 3.12
uv sync --locked
uv run alembic upgrade head
uv run uvicorn app.main:app --reload
```

В PowerShell используйте `Copy-Item` вместо `cp`. Остальные команды одинаковые.
В новом терминале, в backend:

```bash
uv run pytest
uv run ruff check .
```

Swagger: http://localhost:8000/docs. Проверки: `/health` и `/health/db`.
Остановка API: Ctrl+C; затем из корня `docker compose down`.
Без Docker можно установить PostgreSQL 16 отдельно, создать пользователя/БД
из `.env` и выставить DATABASE_URL; Redis для health/API не нужен.

## Конфигурация и ключи

Приложение читает корневой `.env` независимо от текущей директории; переменные
процесса имеют приоритет. `.env.example` содержит названия и безопасные dev-примеры.
Локальные DATABASE_URL/REDIS_URL используют localhost; Compose переопределяет
их на postgres/redis внутри своей сети. При смене пароля/имени БД обновите и
DATABASE_URL для локального запуска. Спецсимволы пароля в URL нужно URL-encode;
простые dev-примеры не предназначены для production.

APP_HOST/APP_PORT используются `uv run python -m app.main` и Docker.
Прямой вызов uvicorn использует свои флаги: `--host 0.0.0.0 --port 8000`.
При смене POSTGRES_PORT измените локальный DATABASE_URL на тот же порт.
При смене REDIS_PORT измените локальный REDIS_URL; внутри Compose порт всегда 6379.

TELEGRAM_API_ID и TELEGRAM_API_HASH каждый разработчик получает отдельно
через https://my.telegram.org или безопасным способом от тимлида.
До задачи Telegram они могут оставаться пустыми. Session — чувствительные данные.
Реальные ключи, api_hash, session-файлы и `.env` содержат секреты: храните их локально
и не выводите в логи.
LLM_PROVIDER/LLM_API_KEY сейчас пустые; provider не выбран.

## Опциональные workers

```bash
docker compose --profile workers up --build -d
```

Worker/Beat не содержат продуктовых или periodic tasks. Redis используется как
broker; result backend пока не нужен. Обычный API не импортирует Celery.
Остановка всего профиля: `docker compose --profile workers down`.

## Bootstrap

Из корня: `bash scripts/bootstrap.sh` или `./scripts/bootstrap.ps1`.
Сначала создайте `.env` и запустите PostgreSQL. Скрипт выполняет locked sync,
миграции и выводит следующие команды; существующий `.env` не перезаписывает.
