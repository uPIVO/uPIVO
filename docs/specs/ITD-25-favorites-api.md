# ITD-25: Favorites API (Избранное пользователя)

**Статус:** Согласовано и реализовано\
**Ветка:** `feat/favorites-api`\
**Дата актуализации:** Октябрь 2026\
**Связанные документы:** `docs/ARCHITECTURE.md`, `docs/DEVELOPMENT.md`, `backend/sql/upivo_schema.sql`

---

## 1. Контекст и цели

Реализация личного избранного пользователя для сервиса Upivo с учетом актуальной архитектуры:
1. **Поддержка публикаций и инфоповодов:** Пользователь может сохранять как одиночные материалы (`FeedItem`), так и целые инфоповоды (`ContentCluster`), объединяющие публикации из разных источников.
2. **Проверка доступности материалов по логике Feed:** Сохранять можно только те публикации и кластеры, которые видны пользователю в ленте (статус `normalized`/`processed`, а источник подключен и активен в `user_sources (enabled = true)`).
3. **Безопасность и авторизация:** Идентификатор пользователя извлекается из контекста авторизации (FastAPI dependency с поддержкой JWT Bearer-токена в `Authorization: Bearer <token>`), исключая подделку `user_id` в параметрах пути.
4. **Идемпотентность и изоляция:** Повторное сохранение не дублирует записи и не затирает заметку. Удаление убирает только связь из избранного и не затрагивает общие публикации `feed_items` и `content_clusters`.

---

## 2. Модель данных (PostgreSQL)

Таблица `favorites`:

| Поле | Тип | Описание / Ограничения |
| --- | --- | --- |
| `id` | `BIGINT GENERATED ALWAYS AS IDENTITY` | Первичный ключ |
| `user_id` | `BIGINT NOT NULL` | Внешний ключ `REFERENCES users(id) ON DELETE CASCADE` |
| `target_type` | `VARCHAR(16) NOT NULL` | Тип сохраненного объекта: `'feed_item'` или `'cluster'` |
| `feed_item_id` | `BIGINT NULL` | `REFERENCES feed_items(id) ON DELETE CASCADE` |
| `cluster_id` | `BIGINT NULL` | `REFERENCES content_clusters(id) ON DELETE CASCADE` |
| `note` | `TEXT NULL` | Пользовательская текстовая заметка к материалу |
| `created_at` | `TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP` | Время сохранения |

### Ограничения и индексы
- **CHECK `chk_favorites_target_reference`**:
  `((target_type = 'feed_item' AND feed_item_id IS NOT NULL AND cluster_id IS NULL) OR (target_type = 'cluster' AND cluster_id IS NOT NULL AND feed_item_id IS NULL))`
- **UNIQUE `uq_favorites_user_feed_item`**: `(user_id, target_type, feed_item_id)`
- **UNIQUE `uq_favorites_user_cluster`**: `(user_id, target_type, cluster_id)`
- **INDEX `idx_favorites_user_id_created_at`**: `(user_id, created_at DESC, id DESC)` для быстрой постраничной выборки.
- **INDEX `idx_favorites_feed_item_id`**, **INDEX `idx_favorites_cluster_id`** для каскадных операций и очистки.

---

## 3. HTTP API (`/api/favorites`)

Все эндпоинты требуют заголовок `Authorization: Bearer <token>`.

### 3.1 POST `/api/favorites` — Добавление в избранное

**Тело запроса:**
```json
{
  "target_type": "feed_item",
  "target_id": 42,
  "note": "Прочитать позже"
}
```
*Поддерживается также передача `"target_type": "cluster", "target_id": 10`, либо обратная совместимость с одиночным полем `"feed_item_id": 42`.*

**Поведение:**
- Проверяется доступность материала:
  - Для `feed_item`: статус `normalized` или `processed`, источник подключен у пользователя (`UserSource.enabled = true`). Иначе — `404 Not Found`.
  - Для `cluster`: хотя бы одна публикация внутри кластера доступна пользователю по правилам выше. Иначе — `404 Not Found`.
- Если запись новая: создается запись, возвращается статус `201 Created`.
- Если материал уже был сохранен этим пользователем: возвращается `200 OK` с существующей записью, исходная `note` **не затирается**.

**Формат ответа:**
```json
{
  "id": 1,
  "user_id": 5,
  "target_type": "feed_item",
  "target_id": 42,
  "feed_item_id": 42,
  "cluster_id": null,
  "note": "Прочитать позже",
  "created_at": "2026-10-27T10:00:00",
  "card": {
    "id": 42,
    "title": "Python 3.14 Release",
    "summary": "Краткое изложение...",
    "published_at": "2026-10-27T09:30:00",
    "source": {
      "id": 1,
      "name": "Habr",
      "type": "habr"
    },
    "original_url": "https://habr.com/p/42",
    "is_cluster": false,
    "cluster_id": null,
    "items_count": 1,
    "related_sources": []
  }
}
```

---

### 3.2 GET `/api/favorites` — Список избранного

**Параметры Query:**
- `limit`: целое число от 1 до 100 (по умолчанию 20)
- `offset`: целое неотрицательное число (по умолчанию 0)

**Ответ (200 OK):**
```json
{
  "items": [ ... ],
  "total": 1,
  "limit": 20,
  "offset": 0
}
```
Элементы отсортированы по времени сохранения (`created_at DESC`, `id DESC`).

---

### 3.3 PATCH `/api/favorites/{favorite_id}` — Обновление заметки

**Тело запроса:**
```json
{
  "note": "Новый текст заметки"
}
```
*Передача `"note": null` очищает заметку.*\
Если запись с указанным `favorite_id` не принадлежит текущему пользователю или отсутствует — возвращается `404 Not Found`.

---

### 3.4 DELETE `/api/favorites/{favorite_id}` — Удаление из избранного

Удаляет запись избранного. Возвращает `204 No Content`.\
Операция идемпотентна. Удаляется только пользовательская связь, исходный материал не затрагивается.

---

### 3.5 DELETE `/api/favorites?target_type=feed_item&target_id=42` — Удаление по типу и ID сущности

Альтернативный удобный эндпоинт для клиентов, знающих только ID сущности из ленты новостей. Возвращает `204 No Content`.
