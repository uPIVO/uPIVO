# Извлечение сущностей из FeedItem

`app.processing.extractors.entity_extractor.extract_entities(item, source_type)` — чистый синхронный извлекатель уже нормализованного `FeedItem`. Функция возвращает JSON-совместимый объект `hashtags`, `mentions`, `urls`, `tools`, `has_media`, `media_types`; не изменяет item, не использует сеть, БД или AI.

```python
from app.processing.extractors.entity_extractor import apply_extracted_entities

# feed_item уже загружен вызывающим слоем; source.type передаётся явно.
apply_extracted_entities(feed_item, source.type)
# Транзакцию, flush и commit выполняет вызывающий код.
```

`apply_extracted_entities` присваивает только `FeedItem.extracted_entities`. Повторный явный вызов вычисляет тот же результат при неизменных входных данных. Самостоятельных вызовов из normalizer/connectors/pipeline нет.

## Локальная проверка

Из каталога `backend`:

```sh
uv run python -m app.processing.extractors
```

CLI попросит тип источника, заголовок, текст одной строкой и необязательный JSON `raw_payload`, затем выведет результат. Запуск не открывает сессию БД и не делает сетевых запросов. Для многострочных примеров можно сохранить объект fixture и запустить:

```sh
uv run python -m app.processing.extractors --fixture /path/to/feed-item.json
```

Fixture содержит `source_type`, `title`, `body_text` и необязательные `original_url`, `raw_payload`, например:

```json
{
  "source_type": "youtube",
  "title": "Обзор #AI",
  "body_text": "Описание и ссылка https://example.org",
  "raw_payload": {
    "kind": "youtube_rss_entry",
    "video_id": "dQw4w9WgXcQ",
    "description": "Дополнительный текст"
  }
}
```

## Извлекаемые признаки и ограничения

- Поиск текста идёт в порядке `title`, `body_text`; для YouTube дополнительно проверяются `description` и `transcript`. Повторы сохраняют первое появление.
- Хештеги поддерживают Unicode; нормализуются в NFC и `casefold`, маркер `#` не сохраняется. Упоминания сохраняют `@` или Reddit-синтаксис `u/`; сравнение/дедупликация регистронезависимы, в результате остаётся написание первого появления. `r/subreddit` и email-адреса не считаются упоминаниями.
- URL из обычного текста, Markdown и HTML распознаются в пределах уже имеющихся строк; принимаются HTTP(S), `www.` и bare-domain ссылки с путём, завершающая пунктуация удаляется. URL не загружаются. Явные верхнеуровневые списки `hashtags`, `mentions`, `urls` учитываются только при подходящих строковых значениях.
- Для Reddit учитываются внешние `url` и `url_overridden_by_dest`, если это не permalink самого поста. `is_video`, `post_hint`, известные `media`/`secure_media.reddit_video`, `oembed` и `gallery_data` позволяют определить присутствующие типы; `is_video=false` само по себе не доказывает отсутствие любого медиа.
- Для YouTube корректный 11-символьный `video_id` при `SourceType.YOUTUBE` явно означает `has_media=true`, `media_types=["video"]`. `link` публикации и `thumbnail_url` не становятся упомянутыми URL.
- Telegram и Habr сейчас не сохраняют достаточно метаданных вложений: без явного сигнала `has_media=null`, `media_types=[]`. У Habr `tags`/`hubs` не переносятся в `hashtags`; текстовые `#теги` извлекаются как обычно.
- При отсутствующем, пустом или неожиданном `raw_payload` извлечение продолжается по нормализованному тексту. Оригинальная ссылка FeedItem не добавляется автоматически, но явная ссылка из текста сохраняется.
- `tools` определяются только по извлечённым URL, не по текстовым названиям продуктов и не по URL самой публикации. Справочник адаптирован из [Siftly `rawjson-extractor.ts`](https://github.com/viperrcrypto/Siftly/blob/main/lib/rawjson-extractor.ts); для него сохранена лицензия MIT в `backend/app/processing/extractors/SIFTLY-LICENSE.txt`. Хост сопоставляется точно или по границе поддомена; path-specific правило GitHub Copilot проверяется до общего GitHub.
