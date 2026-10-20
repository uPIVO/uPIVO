import json
from datetime import datetime

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
                "  ai_metadata TEXT,"
                "  processing_status TEXT DEFAULT 'pending',"
                "  created_at DATETIME,"
                "  updated_at DATETIME"
                ")"
            )
        )

        # Users
        conn.execute(
            text(
                "INSERT INTO users (id, email, password_hash) VALUES "
                "(1, 'u1@test.com', 'h1'), (2, 'u2@test.com', 'h2')"
            )
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
                "(2, 2, 2, 1)"  # user 2 -> Reddit
            )
        )

        # Feed items
        now = datetime(2026, 10, 16, 14, 0).isoformat()
        payload_habr = json.dumps({"tags": ["python", "ai"], "hubs": ["Разработка"]})
        payload_reddit = json.dumps({"tags": ["игры", "steam"], "hubs": []})

        conn.execute(
            text(
                "INSERT INTO feed_items ("
                "  id, source_id, external_id, title, body_text, original_url, "
                "published_at, collected_at, summary, raw_payload"
                ") VALUES "
                "  (101, 1, 'habr:1', 'Новая версия Python вышла', "
                "'Текст про релиз Python...', 'https://habr.com/101', :pub1, :col1, "
                "'Краткая сводка: вышел Python с улучшениями.', :p_habr), "
                "  (102, 2, 'reddit:1', 'Новые игры 2026 года и анонсы', "
                "'Полный текст про игры осени 2026 года...', 'https://reddit.com/102', "
                ":pub2, :col2, NULL, :p_red)"
            ),
            {
                "pub1": now,
                "col1": now,
                "p_habr": payload_habr,
                "pub2": now,
                "col2": now,
                "p_red": payload_reddit,
            },
        )

    yield engine
    engine.dispose()


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


def test_personalized_feed_and_detail(client: TestClient):
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
