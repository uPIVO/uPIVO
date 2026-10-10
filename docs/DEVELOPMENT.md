# Ежедневная разработка

Python-команды выполняются из `backend/`, Compose-команды — из корня.
После обновления файлов проекта:

```bash
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

Выберите одну команду по назначению зависимости. uv обновляет pyproject.toml и uv.lock
вместе. Для обновления существующей зависимости: `uv lock --upgrade-package package-name`,
затем `uv sync --locked`; проверьте изменения и тесты.

## Изменение схемы

Alembic хранит версии структуры базы данных. Migration задаёт последовательность
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
Не выполняйте откат на общей БД без согласования. Обновляйте модель вместе с её миграцией.

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

## Парсер YouTube

Сбор сырых видео с публичного канала через RSS (feedparser, без API-ключа).
Нужны PostgreSQL и `alembic upgrade head`. Из `backend/`:

```bash
uv run python -m app.connectors.youtube "https://www.youtube.com/@GoogleDevelopers"
```

Без записи в БД: добавьте `--dry-run`. Несколько последних роликов (обычно до 15)
пишутся в `sources` + `feed_items` (название, ссылка, дата, описание).
Если у ролика есть субтитры — их текст кладётся в `body_text` и в `raw_payload.transcript`;
если субтитров нет, сохраняется описание, ролик всё равно пишется.
Повторный запуск обновляет те же `external_id`. Пустой канал — source и 0 записей.
Неверный URL завершает процесс с кодом 2; сеть/HTTP — с кодом 1.
Не принимаются search, playlist, watch и OAuth.

## Habr

Предпросмотр свежих статей доступен через `GET /api/habr/preview?limit=3` после
запуска API. Он загружает полный текст, а при недоступности статьи использует
текст из списка публикаций. `HabrService` в `backend/app/services/habr.py` можно
вызвать из backend-кода с обычной SQLAlchemy `Session`; он записывает статьи в
общие таблицы `sources` и `feed_items`. Для предпросмотра база данных не нужна.

## Нормализация контента

После сбора сырых `feed_items` приводит `raw_payload` к полям
title / body_text / original_url / author / published_at и считает `content_hash`
(SHA-256 нормализованных title+body, 64 hex-символа). `raw_payload` не меняется.
Статус: pending → processed или failed. Пустой/битый payload и отсутствие URL —
failed; title/author/дата могут быть пустыми.

Пример без БД, из `backend/`:

```bash
uv run python -m app.processing --fixture tests/fixtures/normalize/sample.json
```

Pending-строки в PostgreSQL (нужны миграции, `alembic upgrade head`):

```bash
uv run python -m app.processing
```

`--dry-run` печатает результат без commit. `--limit N` и `--item-id ID` сужают выборку.

## Сбор под пользователя

Для выбранного пользователя читает `user_sources` (только `enabled`) и собирает
общие `feed_items` по этим источникам. Один канал остаётся одной строкой `sources`.
YouTube идёт через уже существующий `collect_channel`. Остальные типы источников
пропускаются. Нет login/OAuth — пользователь задаётся `--user` (id или email).

Нужны PostgreSQL и `alembic upgrade head`. Пользователь и связи `user_sources`
должны уже быть в базе (колонка `password_hash` есть в схеме, авторизации нет —
для локальной записи достаточно любого непустого значения):

```sql
INSERT INTO users (email, password_hash)
VALUES ('demo@example.com', 'local-dev-not-auth');
-- source_id — строка из sources, например после python -m app.connectors.youtube
INSERT INTO user_sources (user_id, source_id) VALUES (1, 1);
```

Из `backend/`:

```bash
uv run python -m app.services --user demo@example.com
uv run python -m app.services --user 1 --dry-run
uv run python -m app.services --user demo@example.com --normalize
```

`--normalize` после записи прогоняет тот же hook, что `python -m app.processing`,
только по только что собранным строкам. Нет источников / все выключены — `sources=0`
или `status=skipped reason=disabled`, код 0. Неизвестный пользователь — код 2.
Пустой fetch источника — `status=empty`. Сбой чтения YouTube — `status=failed`, код 1.
