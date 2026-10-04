# Ежедневная разработка

Из корня клона создайте ветку:

```bash
git switch -c feature/short-description
```

Python-команды выполняются из `backend/`, Compose-команды — из корня.
После обновления ветки:

```bash
git pull
cd backend
uv sync --locked
uv run alembic upgrade head
```

База должна работать. Если работаете только в Docker, из корня выполните
`docker compose up --build -d --wait`: зависимости и миграции обновятся.

Добавление зависимости:

```bash
uv add package-name
uv add --dev package-name
```

Выберите одну команду по назначению зависимости. Коммитьте pyproject.toml и uv.lock
вместе. Для обновления существующей зависимости: `uv lock --upgrade-package package-name`,
затем `uv sync --locked`; проверьте diff и тесты.

## Изменение схемы

Alembic — «Git для структуры базы данных». Migration хранит последовательность
изменений; upgrade применяет новые версии. Исходная migration пустая и фиксирует baseline.

Добавьте ORM-модель в `app/models`, наследуйте Base из `app.db.base`, импортируйте
модель в `app/models/__init__.py`, чтобы Alembic видел metadata. Затем:

```bash
uv run alembic revision --autogenerate -m "description"
uv run alembic upgrade head
uv run alembic current
uv run alembic check
```

Autogenerate создаёт черновик: проверьте вручную upgrade/downgrade, особенно переименования
и удаление данных. Не меняйте уже применённые командой migrations; добавляйте новые.
Для проверки отката на своей одноразовой БД: `uv run alembic downgrade -1`, затем upgrade.
Не выполняйте откат на общей БД без согласования. Модель и migration идут одним PR.

## Проверки и запуск

```bash
uv run uvicorn app.main:app --reload
```

В другом терминале из backend:

```bash
uv run ruff check .
uv run pytest
```

Для форматирования: `uv run ruff format .`. Tests не требуют живой БД;
успех/ошибка DB endpoint проверяются через override session. Реальный PostgreSQL
и миграции отдельно проверяет CI. Не воспринимайте mock-тест как проверку соединения.

Куда класть код — см. [границы модулей](ARCHITECTURE.md). Не добавляйте новый слой,
пока у него нет понятной задачи. Connector и extractor создаются отдельными задачами.

## Pull Request

Перед PR запустите ruff, pytest и migrations для изменения модели.
Опишите проблему, изменение, способ проверки и оставшиеся ограничения; приложите
номер задачи. Попросите минимум одного reviewer. Не пушьте напрямую в main.
Не коммитьте `.env`, реальные ключи, Telethon session, локальные БД, `.venv` и кеши.
Production CD не настроен; CI проверяет качество на PR и push в main.
