# Архитектура Upivo

Сейчас это один backend. Миграция `0002` добавляет общие таблицы `sources` и
`feed_items`; `0003` — `users` и `user_sources`. Следующие миграции добавляют
состояние синхронизации Telegram и Reddit. Автоматическое расписание пока не настроено.

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
→ future enrichment / clustering / ranking → status=processed
```

Shared ingestion: enabled `user_sources` → DISTINCT `sources` → sync source once
→ shared `feed_items`. `python -m app.services` запускает этот сценарий; один
источник с несколькими подписчиками опрашивается один раз за запуск.

User layer: `User` → `UserSource` → later relevance / ranking / feed / digest.
Персональные действия не создают копии публикаций в `feed_items`.

Общая модель FeedItem хранит `raw_payload JSONB` — исходные данные источника;
`extracted_entities JSONB` предназначено для hashtags, mentions, URLs, tools/products
и других быстро извлечённых сущностей без LLM. Извлечение сущностей остаётся отдельной задачей.

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
Будущий Feed/Digest HTTP API читает сохранённые `feed_items` и не запускает collection.
YouTube и Habr идентифицируются уникальным `external_id` в рамках своего типа.
Connector отвечает за доступ к источнику.
`app.processing` читает pending `feed_items`, мапит `raw_payload` на title / body_text /
original_url / author / published_at, считает `content_hash` и ставит
normalized или failed. `processed` зарезервирован для полного будущего pipeline.
`raw_payload` не переписывается. CLI:
`python -m app.processing`.
Session-файлы Telegram хранятся локально.

`processing/extractors` зарезервирован под будущую Python-адаптацию идеи
rawjson-extractor из Siftly. TypeScript не копируется; extractor требует отдельной задачи.

Прикладные AI-сервисы используют отдельные контракты суммаризации, персональной
релевантности и общей важности из `app/ai/base.py`. OpenRouter создаёт общую выжимку,
Jev / TypeSafe оценивает готовую выжимку: Noul для релевантности и три Score-компонента
для экспериментальной общей важности. Конкретные провайдеры изолированы адаптерами.

Ручной сценарий `app.services.analyze_feed_item` загружает и сохраняет анализ только
для `FeedItem`; AI-запросы выполняются вне DB-транзакций. Репозиторий проверяет актуальность
входа и точного текста выжимки перед записью, сохраняя устаревший ответ отдельно.
Обработка кластеров в БД, общий Celery-конвейер и правила отбора не подключены.
Контракты, CLI, метаданные и ограничения описаны в [документации AI](ai-summarization.md).

## Границы кода

- `api`: HTTP-маршруты: `/health`, `/health/db`, предпросмотр, сохранение и
  просмотр сохранённых статей Habr.
- `core`: env-конфигурация и logging, без секретов в логах.
- `db`: Declarative Base, lazy engine, dependency session; запросы синхронные.
- `models`: ORM `User` / `UserSource` / `Source` / `FeedItem` и состояния
  синхронизации Telegram и Reddit.
- `schemas`: будущие Pydantic-схемы входа/выхода, включая FeedItem.
- `services`: сбор активных общих источников (`python -m app.services`),
  выборка результата для пользователя (`--user`) и сохранение
  Telegram/Reddit с курсорами; `sources` общие, `user_sources` — пользовательские настройки.
- `connectors`: доступ к Telegram/web/RSS/Reddit; YouTube — RSS канала, CLI
  `python -m app.connectors.youtube`. Общий протокол — `SourceConnector`.
- `processing`: нормализация `raw_payload` → поля FeedItem
  (`python -m app.processing`); extractors пока зарезервированы.
- `ai`: provider-neutral интерфейс.
- `workers`: опциональная конфигурация Celery, пока без задач/расписаний.
- `auth`: зарезервировано; схема авторизации не выбрана.

API не подключается к БД при импорте/старте. `/health` проверяет процесс,
`/health/db` проверяет БД и возвращает 503 без деталей ошибок при недоступности.
Compose запускает миграции отдельно перед API; приложение не создаёт таблицы само.
Session закрывается dependency; сценарии сбора выполняют commit/rollback своих записей.
Нет автоматического commit после HTTP-запроса.

Не вводим Firebase/OAuth/JWT до решения команды. Frontend Next.js + TypeScript
начнётся в следующем модуле. Production hosting, auth и продуктовые endpoint
остаются решениями техлида. AI-провайдеры ручного сценария настраиваются через env;
существующая инфраструктура предназначена для разработки.
