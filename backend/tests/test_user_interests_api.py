"""HTTP API интересов пользователя. Без живой PostgreSQL.

SQLite повторяет ограничения таблицы: уникальность без учёта регистра и
непустоту текста проверяет сама БД, а не только предварительный SELECT.
"""

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.session import get_db
from app.main import app
from app.services.user_interests import (
    DuplicateInterestError,
    read_interest_texts,
)
from app.services.user_interests import (
    create_interest as create_service_interest,
)
from app.services.user_interests import (
    update_interest as update_service_interest,
)


@pytest.fixture
def api(monkeypatch):
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
                "CREATE TABLE user_interests ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, "
                "text TEXT NOT NULL, created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, "
                "updated_at DATETIME, "
                "CONSTRAINT ck_user_interests_text_not_blank CHECK (length(trim(text)) > 0))"
            )
        )
        connection.execute(
            text(
                # SQLite-эквивалент индекса PostgreSQL по lower(btrim(text)).
                "CREATE UNIQUE INDEX uq_user_interests_user_text_lower "
                "ON user_interests (user_id, lower(trim(text)))"
            )
        )
        connection.execute(
            text("CREATE INDEX idx_user_interests_user_id ON user_interests (user_id)")
        )
        connection.execute(
            text(
                "CREATE TABLE user_item_scores ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, "
                "feed_item_id INTEGER, cluster_id INTEGER, relevance_score REAL, "
                "rank_score REAL, reason TEXT, "
                "calculated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, "
                "analysis_metadata TEXT)"
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
            yield client, factory, engine
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


def _interest_ids(factory, user_id: int) -> list[int]:
    with factory() as session:
        return list(
            session.scalars(
                text("SELECT id FROM user_interests WHERE user_id = :user_id ORDER BY id"),
                {"user_id": user_id},
            )
        )


def _score_row(factory, user_id: int):
    with factory() as session:
        return (
            session.execute(
                text(
                    "SELECT relevance_score, analysis_metadata FROM user_item_scores "
                    "WHERE user_id = :user_id"
                ),
                {"user_id": user_id},
            )
            .mappings()
            .first()
        )


def _insert_score(factory, user_id: int, *, relevance: float = 0.8) -> None:
    metadata = {
        "schema_version": 2,
        "relevance": {
            "last_good": {
                "status": "current",
                "input_fingerprint": f"v1:{'a' * 64}",
                "source_input_fingerprint": f"input-v1:{'b' * 64}",
                "summary_lineage_fingerprint": f"summary-lineage-v1:{'c' * 64}",
                "interests_fingerprint": f"interests-v1:{'d' * 64}",
                "result": {
                    "relevance": relevance,
                    "fingerprint": f"v1:{'a' * 64}",
                    "provider": "fake",
                    "model": "test",
                },
            }
        },
    }
    with factory() as session:
        session.execute(
            text("DELETE FROM user_item_scores WHERE user_id = :user_id"),
            {"user_id": user_id},
        )
        session.execute(
            text(
                "INSERT INTO user_item_scores (user_id, feed_item_id, relevance_score, "
                "analysis_metadata) VALUES (:user_id, 1, :relevance, :metadata)"
            ),
            {
                "user_id": user_id,
                "relevance": relevance,
                "metadata": json.dumps(metadata),
            },
        )
        session.commit()


def test_unknown_user_is_404(api):
    client, _factory, _engine = api
    listing = client.get("/api/users/999/interests")
    created = client.post("/api/users/999/interests", json={"text": "ИИ"})
    updated = client.patch("/api/users/999/interests/1", json={"text": "ИИ"})
    removed = client.delete("/api/users/999/interests/1")

    assert [response.status_code for response in (listing, created, updated, removed)] == [
        404,
        404,
        404,
        404,
    ]
    assert listing.json() == {"detail": "Пользователь не найден."}


def test_new_user_has_empty_interest_list(api):
    client, factory, _engine = api
    user_id = _add_user(factory, "empty@example.com")

    response = client.get(f"/api/users/{user_id}/interests")

    assert response.status_code == 200
    assert response.json() == []


def test_create_trims_text_and_lists_in_stable_order(api):
    client, factory, _engine = api
    user_id = _add_user(factory, "create@example.com")

    first = client.post(f"/api/users/{user_id}/interests", json={"text": "  ИИ-агенты  "})
    second = client.post(f"/api/users/{user_id}/interests", json={"text": "Электромобили"})
    listing = client.get(f"/api/users/{user_id}/interests")

    assert first.status_code == 201
    assert first.json()["text"] == "ИИ-агенты"
    assert first.json()["user_id"] == user_id
    assert first.json()["id"] > 0
    assert first.json()["created_at"] is not None
    assert first.json()["updated_at"] is None
    assert [item["text"] for item in listing.json()] == ["ИИ-агенты", "Электромобили"]
    assert second.status_code == 201


def test_duplicate_interest_is_409_without_second_row(api):
    client, factory, _engine = api
    user_id = _add_user(factory, "duplicate@example.com")
    client.post(f"/api/users/{user_id}/interests", json={"text": "Новые модели OpenAI"})

    exact = client.post(f"/api/users/{user_id}/interests", json={"text": "Новые модели OpenAI"})
    other_case = client.post(
        f"/api/users/{user_id}/interests", json={"text": "  новые модели openai "}
    )

    assert exact.status_code == 409
    assert other_case.status_code == 409
    assert _count(factory, "user_interests") == 1


def test_blank_text_is_422(api):
    client, factory, _engine = api
    user_id = _add_user(factory, "blank@example.com")

    blank = client.post(f"/api/users/{user_id}/interests", json={"text": "   "})
    empty = client.post(f"/api/users/{user_id}/interests", json={"text": ""})
    missing = client.post(f"/api/users/{user_id}/interests", json={})

    assert (blank.status_code, empty.status_code, missing.status_code) == (422, 422, 422)
    assert _count(factory, "user_interests") == 0


def test_patch_replaces_text_and_noop_patch_keeps_record(api):
    client, factory, _engine = api
    user_id = _add_user(factory, "patch@example.com")
    created = client.post(f"/api/users/{user_id}/interests", json={"text": "ИИ"}).json()

    updated = client.patch(
        f"/api/users/{user_id}/interests/{created['id']}", json={"text": "ИИ-агенты"}
    )
    noop = client.patch(
        f"/api/users/{user_id}/interests/{created['id']}", json={"text": "  ИИ-агенты  "}
    )

    assert updated.status_code == 200
    assert updated.json()["text"] == "ИИ-агенты"
    assert updated.json()["updated_at"] is not None
    assert noop.status_code == 200
    assert noop.json()["text"] == "ИИ-агенты"
    assert noop.json()["updated_at"] == updated.json()["updated_at"]
    assert _count(factory, "user_interests") == 1


def test_patch_to_duplicate_is_409(api):
    client, factory, _engine = api
    user_id = _add_user(factory, "patch-duplicate@example.com")
    client.post(f"/api/users/{user_id}/interests", json={"text": "ИИ"})
    second = client.post(f"/api/users/{user_id}/interests", json={"text": "Роботы"}).json()

    response = client.patch(
        f"/api/users/{user_id}/interests/{second['id']}", json={"text": " ии "}
    )

    assert response.status_code == 409
    listing = client.get(f"/api/users/{user_id}/interests").json()
    assert [item["text"] for item in listing] == ["ИИ", "Роботы"]


def test_db_only_duplicate_post_is_409_and_keeps_session_usable(api, monkeypatch):
    """Дубль, который видит только уникальный индекс БД, не должен давать 500."""
    client, factory, _engine = api
    user_id = _add_user(factory, "db-duplicate@example.com")
    monkeypatch.setattr(
        "app.services.user_interests._reject_duplicate", lambda *_args, **_kwargs: None
    )

    first = client.post(f"/api/users/{user_id}/interests", json={"text": "OpenAI"})
    duplicate = client.post(f"/api/users/{user_id}/interests", json={"text": "  openai  "})
    after = client.post(f"/api/users/{user_id}/interests", json={"text": "Robots"})

    assert first.status_code == 201
    assert duplicate.status_code == 409
    assert duplicate.json() == {"detail": "Такой интерес у пользователя уже существует."}
    assert after.status_code == 201
    assert _count(factory, "user_interests") == 2


def test_db_only_duplicate_patch_is_409_without_changing_text(api, monkeypatch):
    client, factory, _engine = api
    user_id = _add_user(factory, "db-duplicate-patch@example.com")
    monkeypatch.setattr(
        "app.services.user_interests._reject_duplicate", lambda *_args, **_kwargs: None
    )
    client.post(f"/api/users/{user_id}/interests", json={"text": "AI"})
    second = client.post(f"/api/users/{user_id}/interests", json={"text": "Robots"}).json()

    duplicate = client.patch(
        f"/api/users/{user_id}/interests/{second['id']}", json={"text": " ai "}
    )
    renamed = client.patch(
        f"/api/users/{user_id}/interests/{second['id']}", json={"text": "Robotics"}
    )

    assert duplicate.status_code == 409
    assert duplicate.json() == {"detail": "Такой интерес у пользователя уже существует."}
    assert renamed.status_code == 200
    listing = client.get(f"/api/users/{user_id}/interests").json()
    assert [item["text"] for item in listing] == ["AI", "Robotics"]


def test_savepoint_keeps_session_usable_after_db_duplicate(api, monkeypatch):
    """Откат SAVEPOINT не ломает сессию и не оставляет частичных изменений."""
    _client, factory, _engine = api
    user_id = _add_user(factory, "savepoint@example.com")
    monkeypatch.setattr(
        "app.services.user_interests._reject_duplicate", lambda *_args, **_kwargs: None
    )

    with factory() as session:
        create_service_interest(session, user_id, "AI")
        with pytest.raises(DuplicateInterestError):
            create_service_interest(session, user_id, "  ai  ")
        assert read_interest_texts(session, user_id) == ["AI"]

        second = create_service_interest(session, user_id, "Robots")
        with pytest.raises(DuplicateInterestError):
            update_service_interest(session, user_id, second.id, " ai ")
        assert read_interest_texts(session, user_id) == ["AI", "Robots"]

        renamed = update_service_interest(session, user_id, second.id, "Robotics")
        assert renamed.text == "Robotics"


def test_foreign_interest_is_not_accessible(api):
    client, factory, _engine = api
    owner_id = _add_user(factory, "owner@example.com")
    stranger_id = _add_user(factory, "stranger@example.com")
    created = client.post(f"/api/users/{owner_id}/interests", json={"text": "ИИ"}).json()

    stranger_listing = client.get(f"/api/users/{stranger_id}/interests")
    stranger_patch = client.patch(
        f"/api/users/{stranger_id}/interests/{created['id']}", json={"text": "Чужое"}
    )
    stranger_delete = client.delete(f"/api/users/{stranger_id}/interests/{created['id']}")

    assert stranger_listing.json() == []
    assert stranger_patch.status_code == 404
    assert stranger_delete.status_code == 404
    assert client.get(f"/api/users/{owner_id}/interests").json()[0]["text"] == "ИИ"


def test_delete_removes_interest_once(api):
    client, factory, _engine = api
    user_id = _add_user(factory, "delete@example.com")
    created = client.post(f"/api/users/{user_id}/interests", json={"text": "ИИ"}).json()

    removed = client.delete(f"/api/users/{user_id}/interests/{created['id']}")
    again = client.delete(f"/api/users/{user_id}/interests/{created['id']}")

    assert removed.status_code == 204
    assert again.status_code == 404
    assert client.get(f"/api/users/{user_id}/interests").json() == []


def test_database_enforces_uniqueness_and_not_blank(api):
    _client, factory, _engine = api
    user_id = _add_user(factory, "constraints@example.com")
    with factory() as session:
        session.execute(
            text("INSERT INTO user_interests (user_id, text) VALUES (:user_id, 'OpenAI')"),
            {"user_id": user_id},
        )
        session.commit()

    with pytest.raises(IntegrityError):
        with factory() as session:
            session.execute(
                text("INSERT INTO user_interests (user_id, text) VALUES (:user_id, ' openai ')"),
                {"user_id": user_id},
            )
            session.commit()

    with pytest.raises(IntegrityError):
        with factory() as session:
            session.execute(
                text("INSERT INTO user_interests (user_id, text) VALUES (:user_id, '   ')"),
                {"user_id": user_id},
            )
            session.commit()


def test_interest_change_invalidates_current_score_without_fake_zero(api):
    client, factory, _engine = api
    user_id = _add_user(factory, "invalidate@example.com")
    _insert_score(factory, user_id)

    client.post(f"/api/users/{user_id}/interests", json={"text": "ИИ"})

    row = _score_row(factory, user_id)
    metadata = json.loads(row["analysis_metadata"])
    assert row["relevance_score"] is None
    assert metadata["relevance"]["last_good"]["status"] == "stale"
    assert metadata["relevance"]["last_good"]["result"]["relevance"] == 0.8


def test_removing_all_interests_invalidates_current_score(api):
    client, factory, _engine = api
    user_id = _add_user(factory, "invalidate-empty@example.com")
    _insert_score(factory, user_id)
    created = client.post(f"/api/users/{user_id}/interests", json={"text": "ИИ"}).json()
    _insert_score(factory, user_id)

    client.delete(f"/api/users/{user_id}/interests/{created['id']}")

    assert _score_row(factory, user_id)["relevance_score"] is None
    assert client.get(f"/api/users/{user_id}/interests").json() == []


def test_noop_patch_keeps_current_score(api):
    client, factory, _engine = api
    user_id = _add_user(factory, "noop@example.com")
    created = client.post(f"/api/users/{user_id}/interests", json={"text": "ИИ"}).json()
    _insert_score(factory, user_id)

    response = client.patch(
        f"/api/users/{user_id}/interests/{created['id']}", json={"text": " ИИ "}
    )

    assert response.status_code == 200
    assert _score_row(factory, user_id)["relevance_score"] == 0.8


def test_background_hook_runs_only_on_actual_change(api, monkeypatch):
    client, factory, _engine = api
    user_id = _add_user(factory, "hook@example.com")
    calls: list[int] = []
    monkeypatch.setattr(
        "app.services.user_relevance.notify_interests_changed",
        lambda changed_user_id, **_kwargs: calls.append(changed_user_id),
    )

    created = client.post(f"/api/users/{user_id}/interests", json={"text": "ИИ"}).json()
    client.patch(f"/api/users/{user_id}/interests/{created['id']}", json={"text": " ИИ "})
    client.patch(f"/api/users/{user_id}/interests/{created['id']}", json={"text": "Роботы"})
    client.delete(f"/api/users/{user_id}/interests/{created['id']}")
    client.post(f"/api/users/{user_id}/interests", json={"text": "ИИ"})
    client.post(f"/api/users/{user_id}/interests", json={"text": "ии"})

    assert calls == [user_id, user_id, user_id, user_id]
