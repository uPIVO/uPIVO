import json
from datetime import UTC, datetime, timedelta

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
                "  included_updated_at DATETIME"
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

        # 3. User 1 subscriptions
        conn.execute(
            text(
                "INSERT INTO user_sources (id, user_id, source_id, enabled) VALUES "
                "(1, 1, 1, 1), "
                "(2, 1, 2, 1)"
            )
        )

        # 4. Feed Items (опубликованы 2 часа назад)
        two_hours_ago = datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=2)
        payload1 = json.dumps({"tags": ["python", "ai"], "hubs": ["Разработка"]})
        payload2 = json.dumps({"tags": ["игры", "gamedev"], "hubs": ["Игры"]})

        conn.execute(
            text(
                "INSERT INTO feed_items ("
                "  id, source_id, external_id, title, body_text, original_url, "
                "published_at, collected_at, summary, raw_payload, "
                "importance_score, updated_at"
                ") VALUES "
                "  (101, 1, 'habr:1', 'Релиз Python 3.13 с новыми фичами', "
                "'Полный текст про Python...', 'https://habr.com/101', :t1, :t1, "
                "'Саммари релиза Python', :p1, 9.5, NULL), "
                "  (102, 2, 'reddit:1', 'Анонс новой игры осени', "
                "'Полный текст про игры...', 'https://reddit.com/102', :t2, :t2, "
                "'Саммари про игры', :p2, 8.0, NULL)"
            ),
            {
                "t1": two_hours_ago,
                "p1": payload1,
                "t2": two_hours_ago,
                "p2": payload2,
            },
        )
        conn.execute(text("UPDATE feed_items SET processing_status = 'normalized'"))

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


def test_digest_full_lifecycle(client: TestClient, sqlite_engine):
    # 1. Генерация общего дайджеста за 24 часа
    res = client.post(
        "/api/digests/generate",
        json={"user_id": 1, "type": "general", "period_hours": 24},
    )
    assert res.status_code == 201
    digest1 = res.json()
    assert digest1["user_id"] == 1
    assert digest1["type"] == "general"
    assert digest1["items_count"] == 2
    assert "Общий дайджест за" in digest1["title"]
    assert len(digest1["items"]) == 2
    assert digest1["items"][0]["title"] == "Релиз Python 3.13 с новыми фичами"
    # Сортировка по важности: пост с score=9.5 первый
    assert digest1["items"][0]["id"] == 101

    # 2. Повторная генерация дайджеста: посты не должны дублироваться, так как уже были в digest1
    res_repeat = client.post(
        "/api/digests/generate",
        json={"user_id": 1, "type": "general", "period_hours": 24},
    )
    assert res_repeat.status_code == 201
    digest_repeat = res_repeat.json()
    # Нет новых обновлений -> дубли не попали
    assert digest_repeat["items_count"] == 0

    # 3. Обновляем пост 101 (симулируем апдейт статьи)
    with sqlite_engine.begin() as conn:
        now_dt = datetime.now(UTC).replace(tzinfo=None)
        conn.execute(
            text("UPDATE feed_items SET updated_at = :upd WHERE id = 101"),
            {"upd": now_dt},
        )

    # Теперь пост 101 должен снова попасть, так как по нему БЫЛ апдейт!
    res_after_update = client.post(
        "/api/digests/generate",
        json={"user_id": 1, "type": "general", "period_hours": 24},
    )
    assert res_after_update.status_code == 201
    digest_after_upd = res_after_update.json()
    assert digest_after_upd["items_count"] == 1
    assert digest_after_upd["items"][0]["id"] == 101

    # 4. Проверка тематического дайджеста (type="topic", topic="игры")
    # Добавим свежий пост про игры
    with sqlite_engine.begin() as conn:
        now_dt = datetime.now(UTC).replace(tzinfo=None)
        payload = json.dumps({"tags": ["игры", "steam"]})
        conn.execute(
            text(
                "INSERT INTO feed_items (id, source_id, external_id, title, body_text, "
                "original_url, published_at, collected_at, summary, raw_payload, "
                "importance_score, processing_status) VALUES "
                "(103, 2, 'reddit:2', 'Свежий трейлер игры', "
                "'Текст трейлера', 'https://reddit.com/103', :t, :t, "
                "'Саммари трейлера', :p, 7.5, 'normalized')"
            ),
            {"t": now_dt, "p": payload},
        )

    res_topic = client.post(
        "/api/digests/generate",
        json={"user_id": 1, "type": "topic", "topic": "игры", "period_hours": 24},
    )
    assert res_topic.status_code == 201
    digest_topic = res_topic.json()
    assert digest_topic["type"] == "topic"
    assert digest_topic["topic"] == "игры"
    assert digest_topic["items_count"] >= 1
    assert any("игры" in item["title"].lower() for item in digest_topic["items"])

    # 5. Проверка списка плашек на вкладке дайджестов (GET /api/digests)
    res_list = client.get("/api/digests?user_id=1")
    assert res_list.status_code == 200
    cards = res_list.json()
    assert len(cards) >= 3  # все сгенерированные дайджесты доступны
    # Каждая плашка содержит название, превью и дату
    first_card = cards[0]
    assert "title" in first_card
    assert "preview" in first_card
    assert "period_start" in first_card
    assert "items_count" in first_card

    # 6. Клик по плашке (Детальный просмотр GET /api/digests/{id})
    target_id = first_card["id"]
    res_detail = client.get(f"/api/digests/{target_id}?user_id=1")
    assert res_detail.status_code == 200
    detail = res_detail.json()
    assert detail["id"] == target_id
    assert "summary_text" in detail

    with sqlite_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO users (id, email, password_hash) VALUES (2, 'other@test.com', 'h2')")
        )
    assert client.get(f"/api/digests/{target_id}?user_id=2").status_code == 404
    assert client.patch(f"/api/digests/{target_id}/favorite?user_id=2").status_code == 404

    # 7. Избранное: сохранение от удаления
    res_fav = client.patch(f"/api/digests/{target_id}/favorite?user_id=1")
    assert res_fav.status_code == 200
    assert res_fav.json()["is_favorite"] is True

    # 8. Проверка автоудаления дайджестов старше 3 дней
    old_time = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=4)
    with sqlite_engine.begin() as conn:
        # Создаем старый неизбранный дайджест
        conn.execute(
            text(
                "INSERT INTO digests (id, user_id, type, title, summary_text, "
                "period_start, period_end, items_count, is_favorite, created_at) "
                "VALUES (999, 1, 'general', 'Очень старый дайджест', 'Текст', :t, :t, 0, 0, :t)"
            ),
            {"t": old_time},
        )

    # Генерация нового запускает cleanup_expired_digests
    client.post(
        "/api/digests/generate",
        json={"user_id": 1, "type": "general", "period_hours": 24},
    )

    # Проверяем, что дайджест 999 удалён
    res_old = client.get("/api/digests/999?user_id=1")
    assert res_old.status_code == 404

    # А избранный дайджест target_id остался на месте
    res_fav_check = client.get(f"/api/digests/{target_id}?user_id=1")
    assert res_fav_check.status_code == 200
    assert res_fav_check.json()["is_favorite"] is True


def test_digest_excludes_pending_and_failed_items(client: TestClient, sqlite_engine):
    with sqlite_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO feed_items "
                "(id, source_id, external_id, title, body_text, original_url, "
                "processing_status, published_at, collected_at, importance_score) VALUES "
                "(104, 1, 'pending', 'Pending', 'body', 'https://habr.com/104', "
                "'pending', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 10), "
                "(105, 1, 'failed', 'Failed', 'body', 'https://habr.com/105', "
                "'failed', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 10)"
            )
        )
    response = client.post(
        "/api/digests/generate",
        json={"user_id": 1, "type": "general", "period_hours": 24},
    )
    assert response.status_code == 201
    assert {item["id"] for item in response.json()["items"]} == {101, 102}


def test_digest_unknown_user_returns_404(client: TestClient):
    generated = client.post(
        "/api/digests/generate",
        json={"user_id": 999, "type": "general", "period_hours": 24},
    )
    listed = client.get("/api/digests?user_id=999")
    assert generated.status_code == 404
    assert listed.status_code == 404


def test_digest_cluster_deduplication(client: TestClient, sqlite_engine):
    """Publications belonging to the same cluster should be grouped into a single digest card."""
    with sqlite_engine.begin() as conn:
        # Create user 3 with no past digests
        conn.execute(
            text("INSERT INTO users (id, email, password_hash) VALUES (3, 'u3@test.com', 'h3')")
        )
        # Create third source (Telegram)
        conn.execute(
            text(
                "INSERT INTO sources (id, type, name, canonical_url) VALUES "
                "(3, 'telegram', 'Python TG', 'https://t.me/py_chan')"
            )
        )
        # Subscribe user 3 to Habr (source 1), Reddit (source 2), and Telegram (source 3)
        conn.execute(
            text(
                "INSERT INTO user_sources (id, user_id, source_id, enabled) VALUES "
                "(10, 3, 1, 1), "
                "(11, 3, 2, 1), "
                "(12, 3, 3, 1)"
            )
        )

        one_hour_ago = datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=1)
        # Item 106 from Telegram about the same Python release as 101
        conn.execute(
            text(
                "INSERT INTO feed_items ("
                "  id, source_id, external_id, title, body_text, original_url, "
                "published_at, collected_at, summary, importance_score, processing_status"
                ") VALUES "
                "  (106, 3, 'tg:106', 'TG: Вышел релиз Python 3.14', 'Текст в телеге про релиз...', "
                "'https://t.me/py_chan/106', :pub, :col, 'TG саммари', 9.0, 'normalized')"
            ),
            {"pub": one_hour_ago, "col": one_hour_ago},
        )

        # Create ContentCluster for the story
        conn.execute(
            text(
                "INSERT INTO content_clusters (id, title, summary, importance_score, created_at) VALUES "
                "(1, 'Инфоповод: Релиз Python 3.14', 'Сводка кластера: вышел новый Python.', 9.5, CURRENT_TIMESTAMP)"
            )
        )

        # Link 101 and 106 to cluster 1
        conn.execute(
            text(
                "INSERT INTO cluster_items (id, cluster_id, feed_item_id, is_primary) VALUES "
                "(1, 1, 101, 1), "
                "(2, 1, 106, 0)"
            )
        )

    # Generate digest for user 3
    res = client.post(
        "/api/digests/generate",
        json={"user_id": 3, "type": "general", "period_hours": 24},
    )
    assert res.status_code == 201
    data = res.json()

    # User 3 has items 101 (Habr), 102 (Reddit), 106 (Telegram).
    # Since 101 and 106 belong to the same cluster, there should be 2 cards (Cluster 1 + Item 102), not 3!
    assert data["items_count"] == 2
    assert len(data["items"]) == 2

    cluster_card = next(item for item in data["items"] if item["is_cluster"])
    assert cluster_card["cluster_id"] == 1
    assert cluster_card["title"] == "Инфоповод: Релиз Python 3.14"
    assert cluster_card["summary"] == "Сводка кластера: вышел новый Python."
    assert cluster_card["items_count"] == 2
    source_names = {s["name"] for s in cluster_card["related_sources"]}
    assert source_names == {"Habr Python", "Python TG"}

    # Also verify detail view
    digest_id = data["id"]
    res_detail = client.get(f"/api/digests/{digest_id}?user_id=3")
    assert res_detail.status_code == 200
    detail_data = res_detail.json()
    assert detail_data["items_count"] == 2
    assert len(detail_data["items"]) == 2

