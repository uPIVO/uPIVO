"""HTTP API for per-user sources and thematic folders. No live PostgreSQL."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.session import get_db
from app.main import app

CHANNEL_URL = "https://www.youtube.com/channel/UC_x5XG1OV2P6uZZ5FSM9Ttw"
CHANNEL_ID = "UC_x5XG1OV2P6uZZ5FSM9Ttw"


@pytest.fixture
def api():
    engine = create_engine(
        "sqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE users ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT NOT NULL, "
                "password_hash TEXT NOT NULL, created_at DATETIME, updated_at DATETIME)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE sources ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, type TEXT NOT NULL, name TEXT NOT NULL, "
                "canonical_url TEXT NOT NULL, external_id TEXT, last_synced_at DATETIME, "
                "created_at DATETIME)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE user_sources ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, "
                "source_id INTEGER NOT NULL, enabled BOOLEAN NOT NULL, created_at DATETIME, "
                "UNIQUE (user_id, source_id))"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE feed_items ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, source_id INTEGER NOT NULL, "
                "external_id TEXT NOT NULL, title TEXT, body_text TEXT NOT NULL, "
                "original_url TEXT NOT NULL, author TEXT, published_at DATETIME, "
                "collected_at DATETIME, raw_payload TEXT, extracted_entities TEXT, "
                "content_hash TEXT, summary TEXT, importance_score REAL, "
                "processing_status TEXT NOT NULL, created_at DATETIME, updated_at DATETIME)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE folders ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, "
                "name TEXT NOT NULL, description TEXT, parent_id INTEGER, "
                "created_at DATETIME, updated_at DATETIME)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE feed_item_folders ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, folder_id INTEGER NOT NULL, "
                "feed_item_id INTEGER NOT NULL, created_at DATETIME, "
                "UNIQUE (folder_id, feed_item_id))"
            )
        )
    factory = sessionmaker(bind=engine)

    def override_db():
        session = factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_db
    try:
        with TestClient(app) as client:
            yield client, factory
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def _add_user(factory, email: str) -> int:
    with factory() as session:
        session.execute(
            text("INSERT INTO users (email, password_hash) VALUES (:email, 'local-dev-not-auth')"),
            {"email": email},
        )
        session.commit()
        return session.scalar(text("SELECT id FROM users WHERE email = :email"), {"email": email})


def _count(factory, table: str) -> int:
    with factory() as session:
        return session.scalar(text(f"SELECT count(*) FROM {table}"))


def test_unknown_user(api):
    client, _factory = api
    listing = client.get("/api/users/999/sources")
    created = client.post("/api/users/999/topics", json={"name": "AI"})
    assert listing.status_code == 404
    assert listing.json() == {"detail": "Unknown user"}
    assert created.status_code == 404
    assert created.json() == {"detail": "Unknown user"}


def test_empty_lists(api):
    client, factory = api
    user_id = _add_user(factory, "empty@example.com")
    sources = client.get(f"/api/users/{user_id}/sources")
    topics = client.get(f"/api/users/{user_id}/topics")
    assert sources.status_code == 200
    assert sources.json() == []
    assert topics.status_code == 200
    assert topics.json() == []


def test_attach_source_reuses_one_global_row(api):
    client, factory = api
    first_user = _add_user(factory, "one@example.com")
    second_user = _add_user(factory, "two@example.com")
    payload = {"type": "youtube", "url": CHANNEL_URL, "name": "Google for Developers"}

    first = client.post(f"/api/users/{first_user}/sources", json=payload)
    second = client.post(f"/api/users/{second_user}/sources", json=payload)
    again = client.post(f"/api/users/{first_user}/sources", json=payload)

    assert first.status_code == 201
    assert second.status_code == 201
    assert again.status_code == 200
    assert first.json()["source_id"] == second.json()["source_id"] == again.json()["source_id"]
    assert first.json()["enabled"] is True
    assert first.json()["type"] == "youtube"
    assert first.json()["canonical_url"] == CHANNEL_URL
    assert first.json()["external_id"] == CHANNEL_ID
    assert first.json()["name"] == "Google for Developers"
    assert again.json()["id"] == first.json()["id"]
    assert _count(factory, "sources") == 1
    assert _count(factory, "user_sources") == 2


def test_disable_source_keeps_the_global_row(api):
    client, factory = api
    user_id = _add_user(factory, "one@example.com")
    other_user = _add_user(factory, "two@example.com")
    payload = {"type": "youtube", "url": CHANNEL_URL, "name": "Google for Developers"}
    attached = client.post(f"/api/users/{user_id}/sources", json=payload)
    client.post(f"/api/users/{other_user}/sources", json=payload)
    source_id = attached.json()["source_id"]

    disabled = client.patch(f"/api/users/{user_id}/sources/{source_id}", json={"enabled": False})
    listing = client.get(f"/api/users/{user_id}/sources")
    other = client.get(f"/api/users/{other_user}/sources")

    assert disabled.status_code == 200
    assert disabled.json()["enabled"] is False
    assert disabled.json()["source_id"] == source_id
    assert listing.json()[0]["enabled"] is False
    assert other.json()[0]["enabled"] is True
    assert _count(factory, "sources") == 1
    assert _count(factory, "user_sources") == 2


def test_telegram_attach_canonicalizes_aliases_and_rejects_unrelated_urls(api):
    client, factory = api
    user_id = _add_user(factory, "telegram@example.com")
    first = client.post(
        f"/api/users/{user_id}/sources",
        json={"type": "telegram", "url": "https://www.t.me/ExampleChannel"},
    )
    again = client.post(
        f"/api/users/{user_id}/sources",
        json={"type": "telegram", "url": "https://t.me/examplechannel"},
    )
    invalid = client.post(
        f"/api/users/{user_id}/sources",
        json={"type": "telegram", "url": "https://example.com/examplechannel"},
    )
    assert first.status_code == 201
    assert again.status_code == 200
    assert first.json()["source_id"] == again.json()["source_id"]
    assert first.json()["canonical_url"] == "https://t.me/examplechannel"
    assert invalid.status_code == 422
    assert _count(factory, "sources") == 1


def test_reddit_attach_rejects_unrelated_site_url(api):
    client, factory = api
    user_id = _add_user(factory, "reddit@example.com")
    response = client.post(
        f"/api/users/{user_id}/sources",
        json={"type": "reddit", "url": "https://example.com/r/python"},
    )
    assert response.status_code == 422
    assert client.post(
        f"/api/users/{user_id}/sources",
        json={"type": "reddit", "url": "example.com/r/python"},
    ).status_code == 422
    assert _count(factory, "sources") == 0


def test_unlink_removes_only_one_user_relation(api):
    client, factory = api
    one = _add_user(factory, "unlink-one@example.com")
    two = _add_user(factory, "unlink-two@example.com")
    payload = {"type": "youtube", "url": CHANNEL_URL}
    source_id = client.post(f"/api/users/{one}/sources", json=payload).json()["source_id"]
    client.post(f"/api/users/{two}/sources", json=payload)

    assert client.delete(f"/api/users/{one}/sources/{source_id}").status_code == 204
    assert client.get(f"/api/users/{one}/sources").json() == []
    assert len(client.get(f"/api/users/{two}/sources").json()) == 1
    assert _count(factory, "sources") == 1
    assert _count(factory, "user_sources") == 1
    assert client.delete(f"/api/users/{one}/sources/{source_id}").status_code == 404


def test_create_topic(api):
    client, factory = api
    user_id = _add_user(factory, "one@example.com")
    created = client.post(
        f"/api/users/{user_id}/topics",
        json={"name": "  AI  ", "description": "Models"},
    )
    renamed = client.patch(
        f"/api/users/{user_id}/topics/{created.json()['id']}",
        json={"name": "Игры"},
    )
    listing = client.get(f"/api/users/{user_id}/topics")

    assert created.status_code == 201
    assert created.json()["name"] == "AI"
    assert created.json()["description"] == "Models"
    assert created.json()["feed_item_ids"] == []
    assert renamed.status_code == 200
    assert renamed.json()["name"] == "Игры"
    assert renamed.json()["description"] == "Models"
    assert [topic["name"] for topic in listing.json()] == ["Игры"]


def test_put_feed_item_in_topic(api):
    client, factory = api
    user_id = _add_user(factory, "one@example.com")
    attached = client.post(
        f"/api/users/{user_id}/sources",
        json={"type": "youtube", "url": CHANNEL_URL, "name": "Google for Developers"},
    )
    source_id = attached.json()["source_id"]
    with factory() as session:
        session.execute(
            text(
                "INSERT INTO feed_items ("
                "source_id, external_id, body_text, original_url, processing_status"
                ") VALUES (:source_id, 'video-1', 'full text', "
                "'https://www.youtube.com/watch?v=1', 'pending')"
            ),
            {"source_id": source_id},
        )
        session.commit()
        feed_item_id = session.scalar(text("SELECT id FROM feed_items"))

    topic = client.post(f"/api/users/{user_id}/topics", json={"name": "AI"})
    topic_id = topic.json()["id"]
    placed = client.post(
        f"/api/users/{user_id}/topics/{topic_id}/items",
        json={"feed_item_id": feed_item_id},
    )
    listing = client.get(f"/api/users/{user_id}/topics")
    removed = client.delete(f"/api/users/{user_id}/topics/{topic_id}/items/{feed_item_id}")
    after = client.get(f"/api/users/{user_id}/topics")

    assert placed.status_code == 201
    assert placed.json()["feed_item_ids"] == [feed_item_id]
    assert "body_text" not in placed.json()
    assert "title" not in placed.json()
    assert listing.json()[0]["feed_item_ids"] == [feed_item_id]
    assert _count(factory, "feed_items") == 1
    assert removed.status_code == 204
    assert after.json()[0]["feed_item_ids"] == []
