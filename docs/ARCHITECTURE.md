# Архитектура Upivo

Сейчас это один backend, без продуктовых HTTP-endpoint. Миграция `0002` добавляет
таблицы `sources` и `feed_items` для сырого сбора. Остальные этапы обработки —
план для отдельных задач, а не уже реализованная функциональность.

| Технология | Зачем Upivo |
| --- | --- |
| FastAPI | REST API, валидация и автоматические Swagger/OpenAPI |
| PostgreSQL | Основное долговременное хранилище публикаций и будущих настроек |
| SQLAlchemy 2 | Python-модели, SQL-запросы, соединения и sessions |
| Alembic | Последовательные версии структуры базы данных |
| Telethon | Python-клиент Telegram API для будущего сбора публикаций |
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

## Будущий pipeline

```text
external source → collection → raw_payload → normalization → FeedItem
→ cheap entity extraction → extracted_entities → AI processing
→ clustering / ranking → feed / digest
```

Будущая модель FeedItem предусматривает `raw_payload JSONB` — оригинальные данные
источника; `extracted_entities JSONB` — hashtags, mentions, URLs, tools/products
и другие быстро извлечённые сущности без LLM. Формат и полную модель согласует команда.

Telegram API → Telethon → connectors/telegram → будущий FeedItem.
YouTube → канал RSS (feedparser, без Data API) и публичные субтитры (timedtext) →
сырые строки `feed_items` (`raw_payload`, title, url, published_at, body_text).
Это сбор сырья, не дайджест.
Connector отвечает за доступ к источнику.
`app.processing` читает pending `feed_items`, мапит `raw_payload` на title / body_text /
original_url / author / published_at, считает `content_hash` и ставит
processed или failed. `raw_payload` не переписывается. CLI:
`python -m app.processing`.
Session-файлы Telegram хранятся локально.

`processing/extractors` зарезервирован под будущую Python-адаптацию идеи
rawjson-extractor из Siftly. TypeScript не копируется; extractor требует отдельной задачи.

Upivo → LLM Adapter → конкретный provider. Protocol в `app/ai/base.py`
содержит summarize и score_relevance. Provider можно заменить без переписывания
бизнес-логики; реальных API-вызовов нет. Формат результата уточняется с задачей AI.

## Границы кода

- `api`: HTTP-маршруты; сейчас только `/health` и `/health/db` (SELECT 1).
- `core`: env-конфигурация и logging, без секретов в логах.
- `db`: Declarative Base, lazy engine, dependency session; запросы синхронные.
- `models`: ORM `Source` / `FeedItem` и enum `source_type` (включая youtube).
- `schemas`: будущие Pydantic-схемы входа/выхода, включая FeedItem.
- `services`: будущие сценарии продукта, добавлять по задачам.
- `connectors`: доступ к Telegram/web/RSS; YouTube — RSS канала, CLI
  `python -m app.connectors.youtube`. Общий протокол — `SourceConnector`.
- `processing`: нормализация `raw_payload` → поля FeedItem
  (`python -m app.processing`); extractors пока зарезервированы.
- `ai`: provider-neutral интерфейс.
- `workers`: опциональная конфигурация Celery, пока без задач/расписаний.
- `auth`: зарезервировано; схема авторизации не выбрана.

API не подключается к БД при импорте/старте. `/health` проверяет процесс,
`/health/db` проверяет БД и возвращает 503 без деталей ошибок при недоступности.
Compose запускает миграции отдельно перед API; приложение не создаёт таблицы само.
Session закрывается dependency; commit/rollback будущих записей выполняет код сценария.
Нет автоматического commit после HTTP-запроса.

Не вводим Firebase/OAuth/JWT до решения команды. Frontend Next.js + TypeScript
начнётся в следующем модуле. Production hosting, auth, FeedItem и AI provider
остаются решениями техлида. Существующая инфраструктура предназначена для разработки.
