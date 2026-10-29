# Архитектура Upivo

Сейчас это один backend. Миграции добавляют общие `sources` и `feed_items`,
пользовательские связи, состояния сбора, анализ, кластеры, папки и дайджесты.
Четыре независимые миграции после `0005` соединены ревизией `0007_integrated_heads`.
Автоматический общий сбор по расписанию (ITD-84) по умолчанию выключен
(`SCHEDULED_PIPELINE_ENABLED=false`). Полный автоматический AI разрешён без
контроллера расходов (решение 2026-10-29): жёсткой денежной квоты нет, `ai_limit`
не бюджет.

| Технология | Зачем Upivo |
| --- | --- |
| FastAPI | REST API, валидация и автоматические Swagger/OpenAPI |
| PostgreSQL | Основное долговременное хранилище публикаций и будущих настроек |
| SQLAlchemy 2 | Python-модели, SQL-запросы, соединения и sessions |
| Alembic | Последовательные версии структуры базы данных |
| Telethon | Python-клиент Telegram API для ручного консольного сбора публикаций |
| httpx | Скачивает страницу или обращается к внешнему HTTP API |
| trafilatura | Извлекает основной текст статьи, убирая меню/рекламу/шум |
| BeautifulSoup | Точечный разбор HTML там, где trafilatura недостаточно |
| feedparser | Читает RSS/Atom, включая RSS Habr и RSS канала YouTube |
| Redis | Broker фоновых задач / быстрое промежуточное хранилище |
| Celery | Worker, выполняющий долгие фоновые задачи |
| Celery Beat | Планировщик, который по расписанию ставит задачи Celery |
| LLM adapter | Изолирует конкретного AI provider от бизнес-логики |
| Docker/Compose | Единое воспроизводимое окружение API, БД и Redis |
| Python logging | Timestamp, level, logger name, message; LOG_LEVEL через env |

## Pipeline публикаций

```text
collection → raw_payload → normalization → status=normalized
→ deterministic entity extraction → clustering (exact hash + bounded lexical similarity)
→ standalone FeedItem or ContentCluster
→ future cluster AI / personalized ranking → status=processed
```

Shared ingestion: enabled `user_sources` → DISTINCT `sources` → sync source once
→ shared `feed_items`. `python -m app.services` запускает этот сценарий; один
источник с несколькими подписчиками опрашивается один раз за запуск.

User layer: `User` → `UserSource` → персональная лента и дайджест;
`Folder` классифицирует общие публикации для пользователя.
Персональные действия не создают копии публикаций в `feed_items`.
`UserInterest` хранит необязательный глобальный список интересов пользователя.
Пустой список — штатное состояние: персональная оценка не выполняется, а материалы
включённых источников остаются доступны без персональной фильтрации.
DB-backed оценка по интересам переиспользует `ContentUnitAnalysisUseCase` и
`user_item_scores`; правила и стык с ITD-85 описаны в
[документации интересов](user-interests.md).

Общая модель FeedItem хранит `raw_payload JSONB` — исходные данные источника;
`extracted_entities JSONB` предназначено для hashtags, mentions, URLs, tools/products
и других быстро извлечённых сущностей без LLM. Извлечение входит в `app.pipeline`
и доступно отдельной CLI-командой.

Telegram API → Telethon → connectors/telegram → processing/telegram →
services/telegram → FeedItem и курсор PostgreSQL.
Reddit → Arctic Shift API → connectors/reddit → services/reddit → FeedItem и
временной курсор PostgreSQL.
YouTube → канал RSS (feedparser, без Data API) и публичные субтитры (timedtext) →
сырые строки `feed_items` (`raw_payload`, title, url, published_at, body_text).
Это сбор сырья, не дайджест.
`python -m app.services --user` — dev/CLI запуск: синхронизирует только включённые
источники указанного пользователя через общий dispatcher. Готовые результаты
общего запуска можно передать нескольким пользователям без повторного сбора.
Feed/Digest HTTP API читает сохранённые `feed_items` и не запускает collection.
YouTube и Habr идентифицируются уникальным `external_id` в рамках своего типа.
Connector отвечает за доступ к источнику.
`app.processing` читает pending `feed_items`, мапит `raw_payload` на title / body_text /
original_url / author / published_at, считает `content_hash` и ставит
normalized или failed. `processed` ставит общий AI-этап конвейера после актуальных
summary и importance единицы.
`raw_payload` не переписывается. CLI:
`python -m app.processing`.
`app.clustering` склеивает `feed_items` со статусом `normalized` или `processed`
в инфоповоды: одинаковый
`content_hash` — одна история, похожие title и body_text — одна строка
`content_clusters` плюс `cluster_items`. Публикации не удаляются. Summary не
требуется. CLI: `python -m app.clustering`.
`python -m app.pipeline --user` запускает сбор, нормализацию, извлечение сущностей,
склейку и общий AI-этап подряд
для включённых источников одного пользователя; AI-этап и постановка персональной
оценки работают по общей БД. Повторный запуск безопасен и не повторяет платные
вызовы при неизменных входе и конфигурации.

Автоматический путь (ITD-84): Celery Beat → Redis → Worker → задача
`pipeline.run_scheduled` → `asyncio.run(run_shared_pipeline(...))`. Один проход
опрашивает каждый уникальный источник с включённой подпиской ровно один раз, затем
выполняет те же глобальные стадии, что и ручной запуск: нормализацию, извлечение
сущностей, кластеризацию с commit, а затем существующий общий AI-этап и его
штатную асинхронную постановку персональной оценки. Автоматический AI-этап идёт
по всему backlog (`ai_limit=None`), а не по первым 50 единицам. По решению от
2026-10-29 полный automatic AI разрешён без контроллера расходов: жёсткой
денежной квоты нет, а metering/budgets остаются будущей работой
(ITD-84, раздел 4.4). `SCHEDULED_PAID_AI_ENABLED=false` — opt-out: только
бесплатные стадии с причиной `paid_ai_disabled`.
Одновременность обеспечивает сессионный advisory lock PostgreSQL на выделенном
соединении `AUTOCOMMIT` (общий для ручного и автоматического запусков); занятая
блокировка даёт `skipped_locked`, а потеря блокировки прерывает проход без
повторного захвата. Это координация через общий сервер БД, а не распределённый
fencing: проверка идёт между стадиями и по таймеру во время ожидания асинхронной
стадии, но блокирующий синхронный сбор источников не прерывается, а сетевой
разрыв не отменяет уже начатый внешний HTTP-вызов.
Session-файлы Telegram хранятся локально.

`processing/extractors` извлекает сущности только из нормализованных публикаций;
общий pipeline сохраняет результат в `extracted_entities`, не меняя `raw_payload`.

Прикладные AI-сервисы используют отдельные контракты суммаризации, персональной
релевантности и общей важности из `app/ai/base.py`. OpenRouter создаёт общую выжимку,
Jev / TypeSafe оценивает готовую выжимку: Noul для релевантности и три Score-компонента
для экспериментальной общей важности. Конкретные провайдеры изолированы адаптерами.
`app/pipeline/analysis.py` выбирает единицы и последовательно запускает `summary`,
`importance`, подтверждение `processed`, одну постановку персональной оценки в Celery
и запись квитанции постановки в существующие AI-метаданные; промпты, отпечатки и
хранилище AI не дублируются.

Прикладной сценарий `app.services.analyze_feed_item` обрабатывает отдельный FeedItem
или ContentCluster через явный `kind`. Для кластера хранится одна общая выжимка и важность,
участники отдельно не обрабатываются. AI-запросы выполняются вне DB-транзакций;
репозиторий проверяет состав, вход и точный текст выжимки перед записью, сохраняя
устаревший ответ отдельно. Запись членства инвалидирует зависимые результаты.
Автоматический вызов AI из конвейера подключён в ITD-85; планирование через
Celery Beat и запуск общего прохода по расписанию добавлены в ITD-84, но платные
автоматические стадии включаются только после задач бюджета и учёта расходов
(ITD-84, раздел 4.4). API ленты остаётся вне их области.
Чистая функция `services/ranking.py` задаёт provisional MVP-формулу, но её результат
пока не сохраняется и не используется в feed/digest.
Формула: `0.5 × relevance + 0.3 × (importance / 10) + 0.2 × freshness`,
где freshness убывает вдвое каждые 24 часа. Веса предварительные.
Контракты, CLI, метаданные и ограничения описаны в [документации AI](ai-summarization.md).

## Границы кода

- `api`: HTTP-маршруты здоровья, Habr, источников, тем и интересов
  (`/api/users/{user_id}/sources`, `/api/users/{user_id}/topics`,
  `/api/users/{user_id}/interests`), ленты (`/api/feed`) и дайджестов (`/api/digests`).
  Авторизации нет: в пути передаётся `users.id`.
- `core`: env-конфигурация и logging, без секретов в логах.
- `db`: Declarative Base, lazy engine, dependency session; запросы синхронные.
- `models`: ORM `User` / `UserSource` / `UserInterest` / `Source` / `FeedItem` /
  `ContentCluster` / `ClusterItem` / `Folder` / `FeedItemFolder` / `Digest` /
  `DigestItem` / `UserItemScore` и состояния синхронизации Telegram и Reddit.
  Папка — тема общей ленты, не копия публикаций и не избранное.
  `Digest.items_snapshot` хранит готовые карточки на момент создания; `DigestItem`
  сохраняет связи с публикациями для исключения повторов в следующих дайджестах.
  Просмотр старого дайджеста не пересчитывает его по текущему состоянию кластера.
- `schemas`: Pydantic-схемы входа/выхода API.
- `services`: сбор активных общих источников (`python -m app.services`),
  выборка результата для пользователя (`--user`) и сохранение
  Telegram/Reddit с курсорами; `sources` общие, `user_sources` — пользовательские настройки.
- `connectors`: доступ к Telegram/web/RSS/Reddit; YouTube — RSS канала, CLI
  `python -m app.connectors.youtube`. Общий протокол — `SourceConnector`.
- `processing`: нормализация `raw_payload` → поля FeedItem
  (`python -m app.processing`) и отдельное извлечение сущностей.
- `clustering`: склейка инфоповодов в `content_clusters` / `cluster_items`
  (`python -m app.clustering`). Без LLM и без embeddings.
- `pipeline`: один процесс `python -m app.pipeline --user` — сбор, нормализация,
  склейка и общий AI-этап с постановкой персональной оценки в Celery.
- `ai`: provider-neutral интерфейс, ручные сценарии анализа FeedItem и
  репозиторий условного сохранения результатов.
- `workers`: конфигурация Celery и фоновые задачи персональной оценки по интересам;
  расписаний и Celery Beat пока нет.
- `auth`: зарезервировано; схема авторизации не выбрана.

API не подключается к БД при импорте/старте. `/health` проверяет процесс,
`/health/db` проверяет БД и возвращает 503 без деталей ошибок при недоступности.
Compose запускает миграции отдельно перед API; приложение не создаёт таблицы само.
Session закрывается dependency; сценарии сбора выполняют commit/rollback своих записей.
Нет автоматического commit после HTTP-запроса.

Не вводим Firebase/OAuth/JWT до решения команды. Frontend Next.js + TypeScript
начнётся в следующем модуле. Production hosting и auth остаются решениями техлида.
AI-провайдеры ручного сценария настраиваются через env;
существующая инфраструктура предназначена для разработки.
