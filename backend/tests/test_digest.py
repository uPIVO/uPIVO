from datetime import datetime, timedelta, timezone
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
        conn.execute(
            text(
                "CREATE TABLE digests ("
                "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
                "  user_id INTEGER NOT NULL,"
                "  type TEXT NOT NULL DEFAULT 'general',"
                "  title TEXT NOT NULL,"
                "  topic TEXT,"
                "  summary_text TEXT NOT NULL,"
                "  period_start DATETIME NOT NULL,"
                "  period_end DATETIME NOT NULL,"
                "  items_count INTEGER NOT NULL DEFAULT 0,"
                "  is_favorite BOOLEAN NOT NULL DEFAULT 0,"
                "  created_at DATETIME"
                ")"
            )
        )
        conn.execute(
            text(
                "CREATE TABLE digest_items ("
                "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
                "  digest_id INTEGER NOT NULL,"
                "  feed_item_id INTEGER NOT NULL,"
                "  cluster_id INTEGER,"
                "  included_updated_at DATETIME"
                ")"
            )
        )

        # 1. Users
        conn.execute(
            text("INSERT INTO users (id, email, password_hash) VALUES (1, 'u1@test.com', 'h1')")
        )

        # 2. Sources
        conn.execute(
            text(
                "INSERT INTO sources (id, type, name, canonical_url) VALUES "
                "(1, 'habr', 'Habr Python', 'https://habr.com'), "
                "(2, 'reddit', 'r/games', 'https://reddit.com/r/games')"
            )
        )

        # 3. User Subscriptions
        conn.execute(
            text(
                "INSERT INTO user_sources (id, user_id, source_id, enabled) VALUES "
                "(1, 1, 1, 1), "
                "(2, 1, 2, 1)"
            )
        )

        # 4. Feed Items (published within last 12 hours)
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        pub1 = now - timedelta(hours=2)
        pub2 = now - timedelta(hours=4)
        pub3 = now - timedelta(hours=6)

        conn.execute(
            text(
                "INSERT INTO feed_items ("
                "  id, source_id, external_id, title, body_text, original_url, author, published_at, collected_at, "
                "  summary, importance_score, raw_payload, updated_at"
                ") VALUES ("
                "  201, 1, 'habr-201', 'Релиз Python 3.14', 'Подробности о релизе Python...', 'https://habr.com/201', 'author1', "
                "  :p1, :p1, 'Вышел релиз Python 3.14 с JIT.', 9.5, "
                f"  '{json.dumps({'tags': ['python']})}', :p1"
                "), ("
                "  202, 2, 'reddit-202', 'Трейлер новой GTA', 'Анонс трейлера...', 'https://reddit.com/202', 'author2', "
                "  :p2, :p2, 'Вышел новый трейлер GTA.', 8.0, "
                f"  '{json.dumps({'tags': ['игры', 'gta']})}', :p2"
                "), ("
                "  203, 1, 'habr-203', 'Архитектура микросервисов', 'Советы по микросервисам...', 'https://habr.com/203', 'author3', "
                "  :p3, :p3, 'Гайд по архитектуре микросервисов.', 7.0, "
                f"  '{json.dumps({'tags': ['architecture']})}', :p3"
                ")"
            ),
            {"p1": pub1, "p2": pub2, "p3": pub3},
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


def test_digest_full_flow(sqlite_engine, client):
    # 1. Генерация общего дайджеста за 24 часа
    res = client.post(
        "/api/digests/generate",
        json={
            "user_id": 1,
            "type": "general",
            "period_hours": 24,
        },
    )
    assert res.status_code == 201
    digest_data = res.json()
    assert digest_data["type"] == "general"
    assert digest_data["items_count"] == 3
    assert len(digest_data["items"]) == 3
    assert "Общий дайджест" in digest_data["title"]
    # Проверка ранжирования по важности (201 -> 202 -> 203)
    assert digest_data["items"][0]["id"] == 201
    assert digest_data["items"][1]["id"] == 202
    assert digest_data["items"][2]["id"] == 203

    # 2. Повторная генерация без обновлений -> исключение дублей
    res_repeat = client.post(
        "/api/digests/generate",
        json={
            "user_id": 1,
            "type": "general",
            "period_hours": 24,
        },
    )
    assert res_repeat.status_code == 201
    repeat_data = res_repeat.json()
    # Все посты уже попадали в прошлый дайджест, повторных дублей нет!
    assert repeat_data["items_count"] == 0

    # 3. Обновляем пост 201 -> он снова должен попасть в новый дайджест
    now_update = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=5)
    with sqlite_engine.begin() as conn:
        conn.execute(
            text("UPDATE feed_items SET updated_at = :u WHERE id = 201"),
            {"u": now_update},
        )

    res_after_update = client.post(
        "/api/digests/generate",
        json={"user_id": 1, "type": "general", "period_hours": 24},
    )
    assert res_after_update.status_code == 201
    after_update_data = res_after_update.json()
    # Пост 201 обновился, поэтому снова включён
    assert after_update_data["items_count"] == 1
    assert after_update_data["items"][0]["id"] == 201

    # 4. Тематический дайджест (topic="gta")
    res_topic = client.post(
        "/api/digests/generate",
        json={
            "user_id": 1,
            "type": "topic",
            "topic": "gta",
            "period_hours": 24,
        },
    )
    assert res_topic.status_code == 201
    topic_data = res_topic.json()
    assert topic_data["type"] == "topic"
    assert topic_data["topic"] == "gta"

    # 5. Проверка списка плашек на вкладке дайджестов (GET /api/digests)
    res_list = client.get("/api/digests?user_id=1")
    assert res_list.status_code == 200
    cards = res_list.json()
    assert len(cards) >= 3
    first_card = cards[0]
    assert "title" in first_card
    assert "preview" in first_card
    assert "period_start" in first_card
    assert "items_count" in first_card

    # 6. Клик по плашке (Детальный просмотр GET /api/digests/{id})
    target_id = first_card["id"]
    res_detail = client.get(f"/api/digests/{target_id}")
    assert res_detail.status_code == 200
    detail = res_detail.json()
    assert detail["id"] == target_id
    assert "summary_text" in detail

    # 7. Избранное: сохранение от удаления
    res_fav = client.patch(f"/api/digests/{target_id}/favorite")
    assert res_fav.status_code == 200
    assert res_fav.json()["is_favorite"] is True

    # 8. Проверка автоудаления дайджестов старше 3 дней
    old_time = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=4)
    with sqlite_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO digests (id, user_id, type, title, summary_text, period_start, period_end, items_count, is_favorite, created_at) "
                "VALUES (999, 1, 'general', 'Очень старый дайджест', 'Текст', :t, :t, 0, 0, :t)"
            ),
            {"t": old_time},
        )

    client.post(
        "/api/digests/generate",
        json={"user_id": 1, "type": "general", "period_hours": 24},
    )

    res_old = client.get("/api/digests/999")
    assert res_old.status_code == 404

    res_fav_check = client.get(f"/api/digests/{target_id}")
    assert res_fav_check.status_code == 200
    assert res_fav_check.json()["is_favorite"] is True


def test_digest_cluster_deduplication(sqlite_engine, client):
    """Проверяет, что несколько публикаций одного кластера не дублируются в дайджесте."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with sqlite_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO feed_items (id, source_id, external_id, title, body_text, original_url, published_at, collected_at, importance_score, updated_at) "
                "VALUES "
                "(301, 1, 'habr-301', 'Новость А на Хабре', 'Текст новости А', 'https://habr.com/301', :t, :t, 9.0, :t), "
                "(302, 2, 'reddit-302', 'Новость А на Реддите', 'Текст новости А на Реддите', 'https://reddit.com/302', :t, :t, 8.5, :t)"
            ),
            {"t": now},
        )
        conn.execute(
            text(
                "INSERT INTO content_clusters (id, title, summary, importance_score, created_at) "
                "VALUES (10, 'Кластерное событие А', 'Сводка события А', 9.2, :t)"
            ),
            {"t": now},
        )
        conn.execute(
            text(
                "INSERT INTO cluster_items (id, cluster_id, feed_item_id, is_primary) VALUES "
                "(10, 10, 301, 1), "
                "(11, 10, 302, 0)"
            )
        )

    res = client.post(
        "/api/digests/generate",
        json={"user_id": 1, "type": "general", "period_hours": 1},
    )
    assert res.status_code == 201
    data = res.json()

    # Хотя постов два (301 и 302), в дайджест событие вошло один раз!
    cluster_items = [item for item in data["items"] if item["cluster_id"] == 10]
    assert len(cluster_items) == 1
    assert cluster_items[0]["title"] == "КЛАСТЕРНОЕ СОБЫТИЕ А"
