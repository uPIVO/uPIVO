from datetime import datetime
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.session import get_db
from app.main import app


@pytest.fixture
def sqlite_engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE users ("
                "  id INTEGER PRIMARY KEY,"
                "  email TEXT NOT NULL,"
                "  password_hash TEXT NOT NULL,"
                "  created_at DATETIME,"
                "  updated_at DATETIME"
                ")"
            )
        )
        conn.execute(
            text(
                "CREATE TABLE sources ("
                "  id INTEGER PRIMARY KEY,"
                "  type TEXT NOT NULL,"
                "  name TEXT NOT NULL,"
                "  canonical_url TEXT NOT NULL,"
                "  external_id TEXT,"
                "  last_synced_at DATETIME,"
                "  created_at DATETIME"
                ")"
            )
        )
        conn.execute(
            text(
                "CREATE TABLE user_sources ("
                "  id INTEGER PRIMARY KEY,"
                "  user_id INTEGER NOT NULL,"
                "  source_id INTEGER NOT NULL,"
                "  enabled BOOLEAN NOT NULL,"
                "  created_at DATETIME"
                ")"
            )
        )
        conn.execute(
            text(
                "CREATE TABLE feed_items ("
                "  id INTEGER PRIMARY KEY,"
                "  source_id INTEGER NOT NULL,"
                "  external_id TEXT NOT NULL,"
                "  title TEXT,"
                "  body_text TEXT NOT NULL,"
                "  original_url TEXT NOT NULL,"
                "  author TEXT,"
                "  published_at DATETIME,"
                "  collected_at DATETIME,"
                "  raw_payload TEXT,"
                "  extracted_entities TEXT,"
                "  content_hash TEXT,"
                "  summary TEXT,"
                "  importance_score REAL,"
                "  processing_status TEXT DEFAULT 'pending',"
                "  created_at DATETIME,"
                "  updated_at DATETIME"
                ")"
            )
        )
        conn.execute(
            text(
                "CREATE TABLE content_clusters ("
                "  id INTEGER PRIMARY KEY,"
                "  title TEXT,"
                "  summary TEXT,"
                "  importance_score REAL,"
                "  ai_metadata TEXT,"
                "  created_at DATETIME"
                ")"
            )
        )
        conn.execute(
            text(
                "CREATE TABLE cluster_items ("
                "  id INTEGER PRIMARY KEY,"
                "  cluster_id INTEGER NOT NULL,"
                "  feed_item_id INTEGER NOT NULL,"
                "  is_primary BOOLEAN NOT NULL DEFAULT 0"
                ")"
            )
        )

        # Users
        conn.execute(
            text("INSERT INTO users (id, email, password_hash) VALUES (1, 'u1@test.com', 'h1'), (2, 'u2@test.com', 'h2')")
        )

        # Sources
        conn.execute(
            text(
                "INSERT INTO sources (id, type, name, canonical_url) VALUES "
                "(1, 'habr', 'Habr Python', 'https://habr.com'), "
                "(2, 'reddit', 'r/games', 'https://reddit.com/r/games')"
            )
        )

        # User subscriptions
        conn.execute(
            text(
                "INSERT INTO user_sources (id, user_id, source_id, enabled) VALUES "
                "(1, 1, 1, 1), "  # user 1 -> Habr
                "(2, 2, 2, 1)"   # user 2 -> Reddit
            )
        )

        # Feed items
        conn.execute(
            text(
                "INSERT INTO feed_items ("
                "  id, source_id, external_id, title, body_text, original_url, author, published_at, collected_at, "
                "  summary, importance_score, raw_payload"
                ") VALUES ("
                "  101, 1, 'habr-101', 'Новая версия Python вышла', 'Текст про релиз Python...', 'https://habr.com/101', 'Guido', "
                "  '2026-10-18 12:00:00', '2026-10-18 12:05:00', 'Краткая сводка: вышел Python с улучшениями.', 8.5, "
                f"  '{json.dumps({'tags': ['python', 'backend'], 'hubs': ['разработка']})}'"
                "), ("
                "  102, 2, 'reddit-102', 'Новые игры 2026 года и анонсы', 'Полный текст про игры и трейлеры...', 'https://reddit.com/102', 'gamer1', "
                "  '2026-10-18 15:00:00', '2026-10-18 15:05:00', NULL, 6.0, "
                f"  '{json.dumps({'tags': ['игры', 'gamedev']})}'"
                ")"
            )
        )
    return engine


@pytest.fixture
def client(sqlite_engine):
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=sqlite_engine)

    def override_get_db():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def test_feed_api_flow(client):
    # 1. User 1 feed (only Habr items)
    res1 = client.get("/api/feed?user_id=1")
    assert res1.status_code == 200
    data1 = res1.json()
    assert data1["total"] == 1
    post1 = data1["items"][0]
    # Название заглавными буквами
    assert post1["title"] == "НОВАЯ ВЕРСИЯ PYTHON ВЫШЛА"
    # AI Саммари
    assert post1["summary"] == "Краткая сводка: вышел Python с улучшениями."
    assert post1["source"]["type"] == "habr"
    assert post1["original_url"] == "https://habr.com/101"
    assert post1["is_cluster"] is False
    assert post1["publications_count"] == 1

    # 2. User 2 feed (only Reddit items)
    res2 = client.get("/api/feed?user_id=2")
    assert res2.status_code == 200
    data2 = res2.json()
    assert data2["total"] == 1
    post2 = data2["items"][0]
    # Заглавные буквы
    assert post2["title"] == "НОВЫЕ ИГРЫ 2026 ГОДА И АНОНСЫ"
    # Fallback из body_text так как summary=None
    assert post2["summary"].startswith("Полный текст про игры")
    assert post2["source"]["type"] == "reddit"

    # 3. Фильтр по теме (topic="игры")
    res_topic = client.get("/api/feed?user_id=2&topic=игры")
    assert res_topic.status_code == 200
    assert res_topic.json()["total"] == 1

    # Фильтр по теме у юзера 1, где таких постов нет
    res_topic_empty = client.get("/api/feed?user_id=1&topic=игры")
    assert res_topic_empty.status_code == 200
    assert res_topic_empty.json()["total"] == 0

    # 4. Фильтр по источнику (source_type="habr")
    res_source = client.get("/api/feed?user_id=1&source_type=habr")
    assert res_source.status_code == 200
    assert res_source.json()["total"] == 1

    res_source_empty = client.get("/api/feed?user_id=1&source_type=reddit")
    assert res_source_empty.status_code == 200
    assert res_source_empty.json()["total"] == 0

    # 5. Развернутый пост (GET /api/feed/{item_id})
    res_detail = client.get("/api/feed/101")
    assert res_detail.status_code == 200
    detail = res_detail.json()
    assert detail["id"] == 101
    assert detail["body_text"] == "Текст про релиз Python..."
    assert "python" in detail["tags"]
    assert detail["original_url"] == "https://habr.com/101"

    # Несуществующий пост -> 404
    res_404 = client.get("/api/feed/99999")
    assert res_404.status_code == 404


def test_feed_cluster_aggregation_as_single_card(sqlite_engine, client):
    """
    Проверяет требование: несколько публикаций одного события (кластера)
    должны показываться пользователю как одна карточка с объединёнными источниками.
    """
    with sqlite_engine.begin() as conn:
        # Подписываем User 1 также на Reddit (source_id=2)
        conn.execute(
            text("INSERT INTO user_sources (id, user_id, source_id, enabled) VALUES (3, 1, 2, 1)")
        )
        # Добавляем еще одну публикацию про релиз Python на Reddit
        conn.execute(
            text(
                "INSERT INTO feed_items ("
                "  id, source_id, external_id, title, body_text, original_url, author, published_at, collected_at, "
                "  summary, importance_score"
                ") VALUES ("
                "  103, 2, 'reddit-103', 'Python release discussion', 'Reddit discussion on Python release', "
                "  'https://reddit.com/103', 'redditor', '2026-10-18 12:30:00', '2026-10-18 12:35:00', "
                "  'Reddit summary of Python release', 8.0"
                ")"
            )
        )
        # Создаем кластер для этого события
        conn.execute(
            text(
                "INSERT INTO content_clusters (id, title, summary, importance_score, created_at) "
                "VALUES (1, 'Релиз новой версии Python', 'ИИ-выжимка события: сообщество обсуждает релиз Python', 9.0, '2026-10-18 12:00:00')"
            )
        )
        # Привязываем обе публикации (Habr 101 и Reddit 103) к кластеру 1
        conn.execute(
            text(
                "INSERT INTO cluster_items (id, cluster_id, feed_item_id, is_primary) VALUES "
                "(1, 1, 101, 1), "
                "(2, 1, 103, 0)"
            )
        )

    # Запрашиваем ленту пользователя 1
    res = client.get("/api/feed?user_id=1")
    assert res.status_code == 200
    data = res.json()

    # Всего 3 публикации (101, 102, 103), но 101 и 103 объединены в один кластер -> итого 2 карточки!
    assert data["total"] == 2

    # Находим карточку кластера
    card = next(c for c in data["items"] if c["cluster_id"] == 1)

    assert card["cluster_id"] == 1
    assert card["is_cluster"] is True
    assert card["publications_count"] == 2
    # Заголовок взят из кластера в UPPERCASE
    assert card["title"] == "РЕЛИЗ НОВОЙ ВЕРСИИ PYTHON"
    assert card["summary"] == "ИИ-выжимка события: сообщество обсуждает релиз Python"


    # В источниках события присутствуют оба источника (Habr и Reddit)
    source_types = {s["type"] for s in card["sources"]}
    assert "habr" in source_types
    assert "reddit" in source_types

    # В связанных публикациях карточки есть вторая публикация
    assert len(card["related_items"]) == 1
    assert card["related_items"][0]["id"] == 103

    # При просмотре деталей главного поста видны связанные публикации
    detail_res = client.get("/api/feed/101")
    assert detail_res.status_code == 200
    detail = detail_res.json()
    assert detail["cluster_id"] == 1
    assert len(detail["related_publications"]) == 1
    assert detail["related_publications"][0]["id"] == 103
