"""Tests for Favorites API with FeedItem and ContentCluster support."""

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
                "CREATE TABLE favorites ("
                "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
                "  user_id INTEGER NOT NULL,"
                "  target_type TEXT NOT NULL DEFAULT 'feed_item',"
                "  feed_item_id INTEGER,"
                "  cluster_id INTEGER,"
                "  note TEXT,"
                "  created_at DATETIME DEFAULT CURRENT_TIMESTAMP,"
                "  FOREIGN KEY (user_id) REFERENCES users(id),"
                "  FOREIGN KEY (feed_item_id) REFERENCES feed_items(id),"
                "  FOREIGN KEY (cluster_id) REFERENCES content_clusters(id)"
                ")"
            )
        )
        conn.execute(
            text(
                "CREATE UNIQUE INDEX uq_favorites_user_feed ON favorites(user_id, target_type, feed_item_id) "
                "WHERE feed_item_id IS NOT NULL"
            )
        )
        conn.execute(
            text(
                "CREATE UNIQUE INDEX uq_favorites_user_cluster ON favorites(user_id, target_type, cluster_id) "
                "WHERE cluster_id IS NOT NULL"
            )
        )

        # Users: User 1 and User 2
        conn.execute(
            text(
                "INSERT INTO users (id, email, password_hash) VALUES "
                "(1, 'user1@example.com', 'h1'), (2, 'user2@example.com', 'h2')"
            )
        )

        # Sources: Source 1 (Habr) and Source 2 (Reddit)
        conn.execute(
            text(
                "INSERT INTO sources (id, type, name, canonical_url) VALUES "
                "(1, 'habr', 'Habr', 'https://habr.com'), "
                "(2, 'reddit', 'Reddit', 'https://reddit.com')"
            )
        )

        # User 1 is subscribed to Source 1 (enabled) and Source 2 (disabled)
        # User 2 is subscribed to Source 2 (enabled)
        conn.execute(
            text(
                "INSERT INTO user_sources (id, user_id, source_id, enabled) VALUES "
                "(1, 1, 1, 1), "
                "(2, 1, 2, 0), "
                "(3, 2, 2, 1)"
            )
        )

        # Feed items:
        # Item 1: Source 1, normalized
        # Item 2: Source 2, normalized (User 1 has disabled source, User 2 has enabled)
        # Item 3: Source 1, pending (not visible in feed)
        conn.execute(
            text(
                "INSERT INTO feed_items "
                "(id, source_id, external_id, title, body_text, original_url, "
                "processing_status, published_at) VALUES "
                "(1, 1, 'habr-1', 'Python 3.14 Release', 'Article text', "
                "'https://habr.com/p/1', 'normalized', '2026-10-25 10:00:00'), "
                "(2, 2, 'red-2', 'Reddit Discussion', 'Reddit post', "
                "'https://reddit.com/r/python/2', 'normalized', '2026-10-25 11:00:00'), "
                "(3, 1, 'habr-3', 'Unprocessed draft', 'Draft', "
                "'https://habr.com/p/3', 'pending', '2026-10-25 12:00:00'), "
                "(4, 1, 'habr-4', 'Cluster Item Primary', 'Cluster member 1', "
                "'https://habr.com/p/4', 'normalized', '2026-10-25 13:00:00'), "
                "(5, 1, 'habr-5', 'Cluster Item Secondary', 'Cluster member 2', "
                "'https://habr.com/p/5', 'normalized', '2026-10-25 14:00:00')"
            )
        )

        # Content Cluster 10 (contains items 4 and 5 from Source 1)
        # Content Cluster 20 (contains item 2 from Source 2)
        conn.execute(
            text(
                "INSERT INTO content_clusters (id, title, summary) VALUES "
                "(10, 'Major Tech Event Cluster', 'Summary of major tech event'), "
                "(20, 'Reddit Exclusive Story', 'Summary of reddit story')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO cluster_items (id, cluster_id, feed_item_id, is_primary) VALUES "
                "(1, 10, 4, 1), "
                "(2, 10, 5, 0), "
                "(3, 20, 2, 1)"
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


def test_unauthorized_request_rejected(client):
    """Requests without token or dev headers return 401."""
    response = client.get("/api/favorites")
    assert response.status_code == 401


def test_add_feed_item_favorite_success(client):
    """User 1 adds an accessible FeedItem to favorites."""
    response = client.post(
        "/api/favorites",
        headers={"Authorization": "Bearer 1"},
        json={"target_type": "feed_item", "target_id": 1, "note": "Read later"},
    )
    assert response.status_code == 201
    data = response.json()
    assert data["target_type"] == "feed_item"
    assert data["target_id"] == 1
    assert data["note"] == "Read later"
    assert data["card"]["title"] == "Python 3.14 Release"
    assert data["card"]["is_cluster"] is False


def test_add_feed_item_legacy_field_compat(client):
    """User 1 adds favorite using legacy feed_item_id field."""
    response = client.post(
        "/api/favorites",
        headers={"Authorization": "Bearer 1"},
        json={"feed_item_id": 1},
    )
    assert response.status_code == 201
    data = response.json()
    assert data["target_type"] == "feed_item"
    assert data["target_id"] == 1


def test_add_favorite_disabled_source_rejected(client):
    """User 1 cannot save FeedItem 2 because Source 2 is disabled for User 1."""
    response = client.post(
        "/api/favorites",
        headers={"Authorization": "Bearer 1"},
        json={"target_type": "feed_item", "target_id": 2},
    )
    assert response.status_code == 404
    assert "не найдены или недоступны" in response.json()["detail"]


def test_add_favorite_pending_status_rejected(client):
    """User 1 cannot save FeedItem 3 because its processing_status is pending."""
    response = client.post(
        "/api/favorites",
        headers={"Authorization": "Bearer 1"},
        json={"target_type": "feed_item", "target_id": 3},
    )
    assert response.status_code == 404


def test_add_content_cluster_favorite_success(client):
    """User 1 saves an accessible ContentCluster."""
    response = client.post(
        "/api/favorites",
        headers={"Authorization": "Bearer 1"},
        json={"target_type": "cluster", "target_id": 10, "note": "Important Cluster"},
    )
    assert response.status_code == 201
    data = response.json()
    assert data["target_type"] == "cluster"
    assert data["target_id"] == 10
    assert data["cluster_id"] == 10
    assert data["card"]["is_cluster"] is True
    assert data["card"]["title"] == "Major Tech Event Cluster"
    assert data["card"]["items_count"] == 2


def test_add_content_cluster_unavailable_rejected(client):
    """User 1 cannot save Cluster 20 because its items come from disabled source."""
    response = client.post(
        "/api/favorites",
        headers={"Authorization": "Bearer 1"},
        json={"target_type": "cluster", "target_id": 20},
    )
    assert response.status_code == 404


def test_duplicate_add_is_idempotent_preserves_note(client):
    """Repeated save returns 200 and does NOT overwrite original note."""
    headers = {"Authorization": "Bearer 1"}
    # 1. First add
    r1 = client.post(
        "/api/favorites",
        headers=headers,
        json={"target_type": "feed_item", "target_id": 1, "note": "Initial note"},
    )
    assert r1.status_code == 201
    assert r1.json()["note"] == "Initial note"

    # 2. Second add with a different note
    r2 = client.post(
        "/api/favorites",
        headers=headers,
        json={"target_type": "feed_item", "target_id": 1, "note": "Overwriting note attempt"},
    )
    assert r2.status_code == 200
    assert r2.json()["note"] == "Initial note"


def test_independent_favorites_between_users(client):
    """User 1 and User 2 can have independent favorites and notes."""
    # User 1 saves Item 1
    client.post(
        "/api/favorites",
        headers={"Authorization": "Bearer 1"},
        json={"target_type": "feed_item", "target_id": 1, "note": "User 1 note"},
    )
    # User 2 saves Item 2
    client.post(
        "/api/favorites",
        headers={"Authorization": "Bearer 2"},
        json={"target_type": "feed_item", "target_id": 2, "note": "User 2 note"},
    )

    # Check User 1 favorites
    favs_u1 = client.get("/api/favorites", headers={"Authorization": "Bearer 1"}).json()
    assert favs_u1["total"] == 1
    assert favs_u1["items"][0]["note"] == "User 1 note"

    # Check User 2 favorites
    favs_u2 = client.get("/api/favorites", headers={"Authorization": "Bearer 2"}).json()
    assert favs_u2["total"] == 1
    assert favs_u2["items"][0]["note"] == "User 2 note"


def test_update_favorite_note(client):
    """PATCH updates note of existing favorite."""
    headers = {"Authorization": "Bearer 1"}
    r = client.post(
        "/api/favorites",
        headers=headers,
        json={"target_type": "feed_item", "target_id": 1, "note": "Original note"},
    )
    fav_id = r.json()["id"]

    # Update note
    patch_res = client.patch(
        f"/api/favorites/{fav_id}",
        headers=headers,
        json={"note": "Updated note"},
    )
    assert patch_res.status_code == 200
    assert patch_res.json()["note"] == "Updated note"

    # Clear note (set to null)
    clear_res = client.patch(
        f"/api/favorites/{fav_id}",
        headers=headers,
        json={"note": None},
    )
    assert clear_res.status_code == 200
    assert clear_res.json()["note"] is None


def test_delete_favorite_by_id(client):
    """DELETE /api/favorites/{fav_id} removes favorite idempotently."""
    headers = {"Authorization": "Bearer 1"}
    r = client.post(
        "/api/favorites",
        headers=headers,
        json={"target_type": "feed_item", "target_id": 1},
    )
    fav_id = r.json()["id"]

    # Delete
    del_res = client.delete(f"/api/favorites/{fav_id}", headers=headers)
    assert del_res.status_code == 204

    # Second delete is idempotent (returns 204)
    del_res_again = client.delete(f"/api/favorites/{fav_id}", headers=headers)
    assert del_res_again.status_code == 204

    # List is now empty
    favs = client.get("/api/favorites", headers=headers).json()
    assert favs["total"] == 0


def test_delete_favorite_by_target(client):
    """DELETE /api/favorites?target_type=...&target_id=... removes item."""
    headers = {"Authorization": "Bearer 1"}
    client.post(
        "/api/favorites",
        headers=headers,
        json={"target_type": "cluster", "target_id": 10},
    )

    del_res = client.delete("/api/favorites?target_type=cluster&target_id=10", headers=headers)
    assert del_res.status_code == 204

    favs = client.get("/api/favorites", headers=headers).json()
    assert favs["total"] == 0


def test_add_favorite_conflicting_target_id_and_feed_item_id(client):
    """POST /api/favorites with conflicting target_id and feed_item_id returns 422."""
    headers = {"Authorization": "Bearer 1"}
    r = client.post(
        "/api/favorites",
        headers=headers,
        json={"target_id": 1, "feed_item_id": 2},
    )
    assert r.status_code == 422
    assert "must match" in r.text or "Contradictory" in r.text


def test_add_favorite_conflicting_target_id_and_cluster_id(client):
    """POST /api/favorites with conflicting target_id and cluster_id returns 422."""
    headers = {"Authorization": "Bearer 1"}
    r = client.post(
        "/api/favorites",
        headers=headers,
        json={"target_id": 10, "cluster_id": 20},
    )
    assert r.status_code == 422
    assert "must match" in r.text or "Contradictory" in r.text


def test_add_favorite_conflicting_feed_item_and_cluster(client):
    """POST /api/favorites with both feed_item_id and cluster_id returns 422."""
    headers = {"Authorization": "Bearer 1"}
    r = client.post(
        "/api/favorites",
        headers=headers,
        json={"feed_item_id": 1, "cluster_id": 10},
    )
    assert r.status_code == 422
    assert "cannot specify both" in r.text or "Contradictory" in r.text


def test_add_favorite_conflicting_type_feed_item_with_cluster_id(client):
    """POST /api/favorites with target_type=feed_item and cluster_id returns 422."""
    headers = {"Authorization": "Bearer 1"}
    r = client.post(
        "/api/favorites",
        headers=headers,
        json={"target_type": "feed_item", "cluster_id": 10},
    )
    assert r.status_code == 422
    assert "cluster_id cannot be used" in r.text or "Contradictory" in r.text


def test_add_favorite_conflicting_type_cluster_with_feed_item_id(client):
    """POST /api/favorites with target_type=cluster and feed_item_id returns 422."""
    headers = {"Authorization": "Bearer 1"}
    r = client.post(
        "/api/favorites",
        headers=headers,
        json={"target_type": "cluster", "feed_item_id": 1},
    )
    assert r.status_code == 422
    assert "feed_item_id cannot be used" in r.text or "Contradictory" in r.text


def test_add_favorite_matching_target_id_and_feed_item_id_succeeds(client):
    """POST /api/favorites with matching target_id and feed_item_id succeeds."""
    headers = {"Authorization": "Bearer 1"}
    r = client.post(
        "/api/favorites",
        headers=headers,
        json={"target_id": 1, "feed_item_id": 1, "note": "Valid match"},
    )
    assert r.status_code == 201
    data = r.json()
    assert data["target_id"] == 1
    assert data["feed_item_id"] == 1
    assert data["note"] == "Valid match"


def test_add_favorite_concurrency_integrity_error_handled(client, monkeypatch):
    """
    Simulate a race condition where a concurrent request inserts the favorite
    between initial check and insert, raising IntegrityError on flush.
    Service must handle conflict gracefully and return 200 OK without 500.
    """
    from app.services.favorites import FavoritesService

    headers = {"Authorization": "Bearer 1"}

    original_find = FavoritesService._find_favorite
    call_count = 0

    def mock_find_favorite(self, user_id, target_type, target_id):
        nonlocal call_count
        call_count += 1
        # On first check, simulate that record is not found yet (race condition window)
        if call_count == 1:
            # Simultaneously, another worker commits the favorite to DB in parallel
            from app.models.favorite import Favorite
            from app.models.enums import FavoriteTargetType
            concurrent_fav = Favorite(
                user_id=user_id,
                target_type=FavoriteTargetType.FEED_ITEM,
                feed_item_id=4,
                note="Concurrent worker note",
            )
            self.db.add(concurrent_fav)
            self.db.commit()
            return None
        # On subsequent calls (recovery after IntegrityError), return the committed favorite
        return original_find(self, user_id, target_type, target_id)

    monkeypatch.setattr(FavoritesService, "_find_favorite", mock_find_favorite)

    res_concurrent = client.post(
        "/api/favorites",
        headers=headers,
        json={"target_type": "feed_item", "target_id": 4, "note": "Second request note"},
    )
    # Must NOT return 500 Internal Server Error
    assert res_concurrent.status_code == 200
    data = res_concurrent.json()
    assert data["target_id"] == 4
    # Note must NOT be overwritten by the second concurrent request
    assert data["note"] == "Concurrent worker note"

