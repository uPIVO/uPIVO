from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.session import get_db
from app.main import app
from tests.auth_helpers import UserClient as TestClient


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
                "  items_snapshot JSON NOT NULL,"
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
                "  title TEXT NOT NULL,"
                "  summary TEXT NOT NULL,"
                "  importance_score REAL,"
                "  ai_metadata TEXT,"
                "  first_seen_at DATETIME,"
                "  last_item_at DATETIME,"
                "  items_count INTEGER NOT NULL DEFAULT 0,"
                "  created_at DATETIME,"
                "  updated_at DATETIME"
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
                "CREATE TABLE user_item_scores ("
                "  id INTEGER PRIMARY KEY,"
                "  user_id INTEGER NOT NULL,"
                "  feed_item_id INTEGER,"
                "  cluster_id INTEGER,"
                "  relevance_score REAL,"
                "  rank_score REAL,"
                "  reason TEXT,"
                "  calculated_at DATETIME,"
                "  analysis_metadata TEXT"
                ")"
            )
        )
        conn.execute(
            text(
                "CREATE TABLE user_digest_settings ("
                "  user_id INTEGER PRIMARY KEY,"
                "  period_hours INTEGER NOT NULL DEFAULT 24,"
                "  max_items INTEGER NOT NULL DEFAULT 15,"
                "  created_at DATETIME,"
                "  updated_at DATETIME"
                ")"
            )
        )

        # Base seed
        conn.execute(
            text(
                "INSERT INTO users (id, email, password_hash) VALUES "
                "(1, 'u1@test.com', 'h1'), (2, 'u2@test.com', 'h2')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO sources (id, type, name, canonical_url) VALUES "
                "(1, 'habr', 'Habr Python', 'https://habr.com')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO user_sources (user_id, source_id, enabled) VALUES "
                "(1, 1, 1), (2, 1, 1)"
            )
        )

        # 5 feed items: 2 from 12 hours ago, 3 from 48 hours ago
        now = datetime.now(UTC).replace(tzinfo=None)
        h12 = now - timedelta(hours=12)
        h48 = now - timedelta(hours=48)

        conn.execute(
            text(
                "INSERT INTO feed_items (id, source_id, external_id, title, body_text, original_url, published_at, collected_at, importance_score, processing_status) VALUES "
                "(1, 1, 'ext1', 'News 1', 'Content 1', 'http://1', :h12, :h12, 9.0, 'normalized'), "
                "(2, 1, 'ext2', 'News 2', 'Content 2', 'http://2', :h12, :h12, 8.0, 'normalized'), "
                "(3, 1, 'ext3', 'News 3', 'Content 3', 'http://3', :h48, :h48, 7.0, 'normalized'), "
                "(4, 1, 'ext4', 'News 4', 'Content 4', 'http://4', :h48, :h48, 6.0, 'normalized'), "
                "(5, 1, 'ext5', 'News 5', 'Content 5', 'http://5', :h48, :h48, 5.0, 'normalized')"
            ),
            {"h12": h12, "h48": h48},
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


def test_get_default_settings(client: TestClient):
    """При первом запросе возвращаются дефолтные настройки (24 часа, 15 инфоповодов)."""
    res = client.get("/api/digests/settings?user_id=1")
    assert res.status_code == 200
    data = res.json()
    assert data["user_id"] == 1
    assert data["period_hours"] == 24
    assert data["max_items"] == 15


def test_update_settings(client: TestClient):
    """Обновление настроек периода и max_items."""
    res = client.put(
        "/api/digests/settings?user_id=1",
        json={"period_hours": 72, "max_items": 3},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["user_id"] == 1
    assert data["period_hours"] == 72
    assert data["max_items"] == 3
    assert data["updated_at"] is not None

    # Проверка получения сохраненных данных
    res_get = client.get("/api/digests/settings?user_id=1")
    assert res_get.status_code == 200
    assert res_get.json()["period_hours"] == 72
    assert res_get.json()["max_items"] == 3


def test_update_settings_validation(client: TestClient):
    """Валидация диапазонов period_hours (1-168) и max_items (1-100)."""
    # Слишком большой период (> 168)
    res_bad_period = client.put(
        "/api/digests/settings?user_id=1",
        json={"period_hours": 200},
    )
    assert res_bad_period.status_code == 422

    # Отрицательный или нулевой период
    res_zero_period = client.put(
        "/api/digests/settings?user_id=1",
        json={"period_hours": 0},
    )
    assert res_zero_period.status_code == 422

    # Слишком много элементов (> 100)
    res_bad_items = client.put(
        "/api/digests/settings?user_id=1",
        json={"max_items": 150},
    )
    assert res_bad_items.status_code == 422


def test_digest_generation_uses_user_settings(client: TestClient):
    """Генерация дайджеста автоматически использует сохраненные период и max_items."""
    # 1. С дефолтными настройками (24ч): должны попасть только 2 статьи (12-часовой давности)
    res1 = client.post("/api/digests/generate", json={"user_id": 1, "type": "general"})
    assert res1.status_code == 201
    assert res1.json()["items_count"] == 2

    # 2. Обновляем настройки пользователя 2: period_hours=72, max_items=2
    client.put(
        "/api/digests/settings?user_id=2",
        json={"period_hours": 72, "max_items": 2},
    )

    # Для пользователя 2 за 72 часа доступны все 5 статей, но max_items=2 ограничит дайджест 2 публикациями
    res2 = client.post("/api/digests/generate", json={"user_id": 2, "type": "general"})
    assert res2.status_code == 201
    assert res2.json()["items_count"] == 2
    # В дайджест должны попасть самые важные (score 9.0 и 8.0)
    items = res2.json()["items"]
    assert items[0]["id"] == 1
    assert items[1]["id"] == 2


def test_digest_generation_override_settings_via_request(client: TestClient):
    """Явные параметры в запросе переопределяют настройки пользователя."""
    # У пользователя 2 настроено max_items=2, period_hours=72
    client.put(
        "/api/digests/settings?user_id=2",
        json={"period_hours": 72, "max_items": 2},
    )

    # Переопределяем max_items=4 в самом запросе
    res = client.post(
        "/api/digests/generate",
        json={"user_id": 2, "type": "general", "max_items": 4},
    )
    assert res.status_code == 201
    # Должно быть отобрано 4 инфоповода
    assert res.json()["items_count"] == 4

