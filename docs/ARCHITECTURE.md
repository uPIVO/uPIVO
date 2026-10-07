# Архитектура Upivo

Сейчас это один backend без продуктовых endpoint. В коде предусмотрены общие таблицы
источников и публикаций; они создаются после применения миграций. Telegram-сбор работает
только через ручную CLI-команду. Остальные этапы — план для отдельных задач.

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
| feedparser | Читает RSS/Atom, включая RSS Habr |
| Redis | Broker фоновых задач / быстрое промежуточное хранилище |
| Celery | Worker, выполняющий долгие фоновые задачи |
| Celery Beat | Планировщик, который по расписанию ставит задачи Celery |
| LLM adapter | Изолирует конкретного AI provider от бизнес-логики |
| Docker/Compose | Единое воспроизводимое окружение API, БД и Redis |
| Python logging | Timestamp, level, logger name, message; LOG_LEVEL через env |

## Pipeline публикаций

```text
external source → collection → raw_payload → normalization → FeedItem
→ cheap entity extraction → extracted_entities → AI processing
→ clustering / ranking → feed / digest
```

Общая модель FeedItem хранит `raw_payload JSONB` — исходные данные источника;
`extracted_entities JSONB` предназначено для hashtags, mentions, URLs, tools/products
и других быстро извлечённых сущностей без LLM. Извлечение сущностей остаётся отдельной задачей.

Telegram API → Telethon → connectors/telegram → processing/telegram → services/telegram
→ FeedItem и курсор PostgreSQL. Connector отвечает за доступ к источнику, processing
нормализует данные, а services выполняет сценарий и транзакцию сохранения.
Сбор и нормализация Telegram реализованы; другие коннекторы и парсеры остаются
отдельными задачами. Session-файлы хранятся локально.

`processing/extractors` зарезервирован под будущую Python-адаптацию идеи
rawjson-extractor из Siftly. TypeScript не копируется; extractor требует отдельной задачи.

Upivo → LLM Adapter → конкретный provider. Protocol в `app/ai/base.py`
содержит summarize и score_relevance. Provider можно заменить без переписывания
бизнес-логики; реальных API-вызовов нет. Формат результата уточняется с задачей AI.

## Границы кода

- `api`: HTTP-маршруты; сейчас только `/health` и `/health/db` (SELECT 1).
- `core`: env-конфигурация и logging, без секретов в логах.
- `db`: Declarative Base, lazy engine, dependency session; запросы синхронные.
- `models`: общие ORM-модели источников, публикаций и курсора Telegram; импортируются в models/__init__.py для Alembic.
- `schemas`: будущие Pydantic-схемы входа/выхода, включая FeedItem.
- `services`: сценарии продукта и транзакции; сейчас здесь сценарий сохранения Telegram.
- `connectors`: доступ к Telegram/web/RSS.
- `processing`: normalization, extraction и дальнейшая обработка.
- `ai`: provider-neutral интерфейс.
- `workers`: опциональная конфигурация Celery, пока без задач/расписаний.
- `auth`: зарезервировано; схема авторизации не выбрана.

API не подключается к БД при импорте/старте. `/health` проверяет процесс,
`/health/db` проверяет БД и возвращает 503 без деталей ошибок при недоступности.
Compose запускает миграции отдельно перед API; приложение не создаёт таблицы само.
Session закрывается dependency; сценарии сбора выполняют commit/rollback своих записей.
Нет автоматического commit после HTTP-запроса.

Не вводим Firebase/OAuth/JWT до решения команды. Frontend Next.js + TypeScript
начнётся в следующем модуле. Production hosting, auth, продуктовые endpoint и AI provider
остаются решениями техлида. Существующая инфраструктура предназначена для разработки.
