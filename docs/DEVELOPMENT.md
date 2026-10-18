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
Повторный запуск обновляет те же `external_id`. Субтитры с YouTube не качаются снова,
если текст уже лежит в `raw_payload.transcript`. Пустой канал — source и 0 записей.
Неверный URL завершает процесс с кодом 2; сеть/HTTP — с кодом 1.
Не принимаются search, playlist, watch и OAuth.

## Habr

Предпросмотр свежих статей доступен через `GET /api/habr/preview?limit=3` после
запуска API. Он загружает полный текст, а при недоступности статьи использует
текст из списка публикаций. `HabrService` в `backend/app/services/habr.py` можно
вызвать из backend-кода с обычной SQLAlchemy `Session`; он записывает статьи в
общие таблицы `sources` и `feed_items`. Для предпросмотра база данных не нужна.
`POST /api/habr/sync` с JSON `{"pages": 1}` запускает сохранение, а
`GET /api/habr/saved` показывает сохранённые статьи. Для этих запросов нужна БД.
Если статьи получены, но ни одну не удалось обработать, предпросмотр возвращает
HTTP 502; ошибки отдельных статей записываются в лог.

## Reddit

Предпросмотр и сохранение публикаций subreddit запускаются отдельными CLI-командами.
Публикации попадают в общие `sources` и `feed_items`, а временной курсор хранится
в `reddit_sync_state`. Команды, ограничения источника и повторная загрузка описаны
в [инструкции Reddit](REDDIT.md).

## Нормализация контента

После сбора сырых `feed_items` приводит `raw_payload` к полям
title / body_text / original_url / author / published_at и считает `content_hash`
(SHA-256 нормализованных title+body, 64 hex-символа). `raw_payload` не меняется.
Статус: pending → normalized или failed. Пустой/битый payload и отсутствие URL —
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

Основной запуск синхронизирует каждый источник с хотя бы одной включённой связью
`user_sources` один раз, независимо от числа подписчиков. Поддерживаются YouTube,
Telegram, Reddit и Habr; публикации остаются в общих `feed_items`.
`--user` синхронизирует только включённые источники выбранного пользователя через
общий dispatcher; источники других пользователей не опрашиваются. Основной запуск
без `--user` по-прежнему собирает каждый DISTINCT enabled Source один раз. Нет
login/OAuth — пользователь задаётся id или email. Будущий Feed/Digest HTTP API
будет только читать сохранённые публикации без запуска collection.

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
uv run python -m app.services --user 1
```

Нет источников / все выключены — `sources=0`, код 0. Неизвестный пользователь — код 2.
Пустой fetch источника — `status=empty`; ошибка сбора — `status=failed`, код 1.
Нормализация запускается отдельно: `uv run python -m app.processing`.

## Дедупликация и склейка инфоповодов

После сбора (и, если хеш ещё нужен, после нормализации) одинаковые и близкие
публикации становятся одним инфоповодом. Строки `feed_items` не удаляются.
Миграция `0006` добавляет только `content_clusters` и `cluster_items`.

Точный дубль — одинаковый непустой `content_hash`. Если хеш пустой, тот же
очищенный title + body тоже считается точным дублем. Разные тексты об одном
событии сравниваются только по title и body_text: оценка
`0.8 * cosine(слова заголовка) + 0.2 * cosine(слова текста)`, порог `0.55`,
и в заголовках должно быть минимум 2 общих содержательных слова. Почти дословное
тело (cosine ≥ 0.85 и 12+ общих слов) склеивается даже при разных заголовках.
Короткий повторяющийся футер не склеивает ролики. Summary и importance не
читаются и могут быть пустыми. Одиночная публикация не получает кластер.

В кластере один `is_primary`: самый длинный очищенный `body_text`, при равенстве
более новый `published_at`. `content_clusters.title` берётся из primary.
`content_clusters.summary` остаётся пустым; повторный запуск не создаёт вторую
строку кластера и не затирает summary, если оно уже было.

Пример без БД, из `backend/`:

```bash
uv run python -m app.clustering --fixture tests/fixtures/cluster/sample.json
```

Все текущие `feed_items` в PostgreSQL (`alembic upgrade head`):

```bash
uv run python -m app.clustering
uv run python -m app.clustering --dry-run
```

Печать: id кластера, id primary, заголовки, пометка `exact` или `similar`.
`--dry-run` не пишет строки. Пустая лента — `clusters=0`. Неверный fixture — код 2,
ошибка БД — код 1.

## Конвейер публикаций

Один запуск для пользователя, у которого источники уже включены: сбор сырья,
нормализация pending `feed_items`, склейка в `content_clusters` / `cluster_items`.
Этапы не переписываются, вызываются как есть. Нормализация и склейка смотрят
на общую ленту `feed_items`, как отдельные команды `app.processing` и
`app.clustering`, а не только на строки этого пользователя. Повторный запуск
идемпотентен: сбор обновляет те же `external_id`, нормализация берёт только
`pending`, склейка обновляет уже существующие кластеры. Нет включённых
источников — сбор не вызывается, нормализация и склейка общей ленты всё равно
выполняются.

Не входит в этот запуск: LLM summary, importance, digest, ранжирование ленты,
HTTP API, frontend, Celery Beat. После склейки в коде оставлено место для
будущего summary, вызова нет.

Нужны PostgreSQL и `alembic upgrade head`. Пользователь и `user_sources` —
как для `python -m app.services --user`. Из `backend/`:

```bash
uv run python -m app.pipeline --user demo@example.com
uv run python -m app.pipeline --user 1 --limit 5
uv run python -m app.pipeline --user 1 --dry-run
uv run python -m app.pipeline --fixture tests/fixtures/pipeline/sample.json
```

`--dry-run` не опрашивает источники и откатывает нормализацию и склейку.
`--fixture` гоняет те же чистые шаги без базы. Неизвестный пользователь — код 2.
Ошибка сбора — код 1, после того как pending-строки уже нормализованы и склеены.
Нет `--user` и нет `--fixture` — код 2.
