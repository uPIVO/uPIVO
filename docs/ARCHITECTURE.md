# Архитектура Upivo

Сейчас это один backend. Миграции добавляют общие `sources` и `feed_items`,
пользовательские связи, состояния сбора, анализ, кластеры, папки и дайджесты.
Четыре независимые миграции после `0005` соединены ревизией `0007_integrated_heads`.
Автоматическое расписание пока не настроено.

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
normalized или failed. `processed` зарезервирован для полного будущего pipeline.
`raw_payload` не переписывается. CLI:
`python -m app.processing`.
`app.clustering` склеивает `feed_items` со статусом `normalized` или `processed`
в инфоповоды: одинаковый
`content_hash` — одна история, похожие title и body_text — одна строка
`content_clusters` плюс `cluster_items`. Публикации не удаляются. Summary не
требуется. CLI: `python -m app.clustering`.
`python -m app.pipeline --user` запускает сбор, нормализацию, извлечение сущностей
и склейку подряд
для включённых источников одного пользователя. Повторный запуск безопасен.
LLM summary здесь не вызывается.
Session-файлы Telegram хранятся локально.

`processing/extractors` извлекает сущности только из нормализованных публикаций;
общий pipeline сохраняет результат в `extracted_entities`, не меняя `raw_payload`.

Прикладные AI-сервисы используют отдельные контракты суммаризации, персональной
релевантности и общей важности из `app/ai/base.py`. OpenRouter создаёт общую выжимку,
Jev / TypeSafe оценивает готовую выжимку: Noul для релевантности и три Score-компонента
для экспериментальной общей важности. Конкретные провайдеры изолированы адаптерами.

Прикладной сценарий `app.services.analyze_feed_item` обрабатывает отдельный FeedItem
или ContentCluster через явный `kind`. Для кластера хранится одна общая выжимка и важность,
участники отдельно не обрабатываются. AI-запросы выполняются вне DB-транзакций;
репозиторий проверяет состав, вход и точный текст выжимки перед записью, сохраняя
устаревший ответ отдельно. Запись членства инвалидирует зависимые результаты.
Автоматический вызов AI из конвейера, общий Celery-конвейер и правила отбора не подключены.
Чистая функция `services/ranking.py` задаёт provisional MVP-формулу, но её результат
пока не сохраняется и не используется в feed/digest.
Формула: `0.5 × relevance + 0.3 × (importance / 10) + 0.2 × freshness`,
где freshness убывает вдвое каждые 24 часа. Веса предварительные.
Контракты, CLI, метаданные и ограничения описаны в [документации AI](ai-summarization.md).

## Границы кода

- `api`: HTTP-маршруты здоровья, Habr, источников и тем
  (`/api/users/{user_id}/sources`, `/api/users/{user_id}/topics`), ленты
  (`/api/feed`) и дайджестов (`/api/digests`).
  Авторизации нет: в пути передаётся `users.id`.
- `core`: env-конфигурация и logging, без секретов в логах.
- `db`: Declarative Base, lazy engine, dependency session; запросы синхронные.
- `models`: ORM `User` / `UserSource` / `Source` / `FeedItem` / `ContentCluster` /
  `ClusterItem` / `Folder` / `FeedItemFolder` / `Digest` / `DigestItem` /
  `UserItemScore` и состояния синхронизации Telegram и Reddit.
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
  склейка. Summary не вызывается.
- `ai`: provider-neutral интерфейс и ручные сценарии анализа FeedItem.
- `workers`: опциональная конфигурация Celery, пока без задач/расписаний.
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
