"""HTTP API обратной связи по публикациям и инфоповодам. Без живой PostgreSQL.

SQLite повторяет ограничения таблицы: XOR единицы, уникальность пары и значение
реакции задаёт схема, а доступность единицы проверяет общий предикат ленты
(включённый источник, статус обработки, участие в инфоповоде).
"""

from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.db.session import get_db
from app.main import app
from app.models.enums import FeedbackType
from app.schemas.feedback import FeedbackKind
from app.services.feedback import set_feedback


@pytest.fixture
def api(tmp_path):
    # Файловая БД даёт отдельные соединения: как в PostgreSQL, ответ не зависит
    # от чужих изменений той же строки сразу после commit.
    engine = create_engine(
        f"sqlite:///{tmp_path / 'feedback.db'}",
        connect_args={"check_same_thread": False},
    )
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE users ("
                "id INTEGER PRIMARY KEY, email TEXT NOT NULL, password_hash TEXT NOT NULL, "
                "created_at DATETIME, updated_at DATETIME)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE user_sources ("
                "id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, source_id INTEGER NOT NULL, "
                "enabled BOOLEAN NOT NULL, created_at DATETIME)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE feed_items ("
                "id INTEGER PRIMARY KEY, source_id INTEGER NOT NULL, external_id TEXT NOT NULL, "
                "title TEXT, body_text TEXT NOT NULL, original_url TEXT NOT NULL, "
                "published_at DATETIME, collected_at DATETIME, processing_status TEXT)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE content_clusters ("
                "id INTEGER PRIMARY KEY, title TEXT, summary TEXT, created_at DATETIME)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE cluster_items ("
                "id INTEGER PRIMARY KEY, cluster_id INTEGER NOT NULL, "
                "feed_item_id INTEGER NOT NULL, is_primary BOOLEAN NOT NULL DEFAULT 0)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE feedback ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, "
                "feed_item_id INTEGER, cluster_id INTEGER, reaction TEXT NOT NULL, "
                "created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at DATETIME, "
                "CONSTRAINT ck_feedback_exactly_one_target CHECK ("
                "(feed_item_id IS NOT NULL AND cluster_id IS NULL) OR "
                "(feed_item_id IS NULL AND cluster_id IS NOT NULL)), "
                "CONSTRAINT uq_feedback_user_feed_item UNIQUE (user_id, feed_item_id), "
                "CONSTRAINT uq_feedback_user_cluster UNIQUE (user_id, cluster_id))"
            )
        )
        connection.execute(
            text(
                "CREATE INDEX idx_feedback_user_created_at ON feedback "
                "(user_id, created_at DESC, id DESC)"
            )
        )
        _seed(connection)

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


def _seed(connection) -> None:
    """Пользователь 1 подписан на источник 1, источник 3 у него выключен.

    Публикации и инфоповоды покрывают все правила показа: одиночная карточка,
    участник кластера, выключенный источник, чужой источник, неготовый материал.
    """
    connection.execute(
        text(
            "INSERT INTO users (id, email, password_hash) VALUES "
            "(1, 'u1@test.local', 'h1'), (2, 'u2@test.local', 'h2')"
        )
    )
    connection.execute(
        text(
            "INSERT INTO user_sources (id, user_id, source_id, enabled) VALUES "
            "(1, 1, 1, 1), (2, 2, 2, 1), (3, 1, 3, 0)"
        )
    )
    moment = datetime(2026, 10, 28, 10, 0).isoformat()
    connection.execute(
        text(
            "INSERT INTO feed_items ("
            "id, source_id, external_id, title, body_text, original_url, "
            "published_at, collected_at, processing_status) VALUES "
            "(101, 1, 'f101', 'Одиночная', 'Текст 101', 'https://example.test/101', "
            ":moment, :moment, 'normalized'), "
            "(102, 1, 'f102', 'Участник', 'Текст 102', 'https://example.test/102', "
            ":moment, :moment, 'normalized'), "
            "(103, 1, 'f103', 'Неготовая', 'Текст 103', 'https://example.test/103', "
            ":moment, :moment, 'pending'), "
            "(104, 3, 'f104', 'Выключенный', 'Текст 104', 'https://example.test/104', "
            ":moment, :moment, 'normalized'), "
            "(105, 2, 'f105', 'Чужая', 'Текст 105', 'https://example.test/105', "
            ":moment, :moment, 'normalized'), "
            "(106, 3, 'f106', 'Чужой кластер', 'Текст 106', 'https://example.test/106', "
            ":moment, :moment, 'normalized'), "
            "(107, 1, 'f107', 'Участник 101', 'Текст 107', 'https://example.test/107', "
            ":moment, :moment, 'processed')"
        ),
        {"moment": moment},
    )
    connection.execute(
        text(
            "INSERT INTO content_clusters (id, title, summary) VALUES "
            "(1, 'Свой кластер', 'Сводка 1'), "
            "(2, 'Только из выключенного', 'Сводка 2'), "
            "(101, 'Кластер со своим участником', 'Сводка 101')"
        )
    )
    connection.execute(
        text(
            "INSERT INTO cluster_items (id, cluster_id, feed_item_id, is_primary) VALUES "
            "(1, 1, 102, 1), (2, 2, 106, 1), (3, 101, 107, 1)"
        )
    )


def _put(client: TestClient, user_id: int, kind: str, unit_id: int, reaction: str):
    url = f"/api/users/{user_id}/feedback/{kind}/{unit_id}"
    return client.put(url, json={"reaction": reaction})


def _row(factory, user_id: int, column: str, unit_id: int):
    with factory() as session:
        return (
            session.execute(
                text(
                    "SELECT id, reaction, created_at, updated_at FROM feedback "
                    f"WHERE user_id = :user_id AND {column} = :unit_id"
                ),
                {"user_id": user_id, "unit_id": unit_id},
            )
            .mappings()
            .first()
        )


def _count(factory, table: str) -> int:
    with factory() as session:
        return session.scalar(text(f"SELECT count(*) FROM {table}"))


def test_put_creates_then_repeats_without_changing_updated_at(api):
    client, factory, _ = api
    created = _put(client, 1, "feed_item", 101, "interested")
    assert created.status_code == 201
    body = created.json()
    assert set(body) == {
        "id",
        "user_id",
        "kind",
        "unit_id",
        "reaction",
        "created_at",
        "updated_at",
    }
    assert body["user_id"] == 1
    assert body["kind"] == "feed_item"
    assert body["unit_id"] == 101
    assert body["reaction"] == "interested"
    assert body["updated_at"] is None
    assert _count(factory, "feedback") == 1

    repeated = _put(client, 1, "feed_item", 101, "interested")
    assert repeated.status_code == 200
    assert repeated.json()["id"] == body["id"]
    assert repeated.json()["created_at"] == body["created_at"]
    assert repeated.json()["updated_at"] is None
    row = _row(factory, 1, "feed_item_id", 101)
    assert row["reaction"] == "interested"
    assert row["updated_at"] is None
    assert _count(factory, "feedback") == 1


def test_put_replaces_reaction_in_same_row_without_history(api):
    client, factory, _ = api
    created = _put(client, 1, "feed_item", 101, "interested")
    assert created.status_code == 201

    replaced = _put(client, 1, "feed_item", 101, "not_interested")
    assert replaced.status_code == 200
    body = replaced.json()
    assert body["id"] == created.json()["id"]
    assert body["reaction"] == "not_interested"
    assert body["created_at"] == created.json()["created_at"]
    assert body["updated_at"] is not None
    assert _count(factory, "feedback") == 1
    assert client.get("/api/users/1/feedback/feed_item/101").json()["reaction"] == "not_interested"


def test_delete_is_physical_and_idempotent(api):
    client, factory, _ = api
    assert _put(client, 1, "cluster", 1, "interested").status_code == 201

    first = client.delete("/api/users/1/feedback/cluster/1")
    assert first.status_code == 204
    assert first.content == b""
    assert _count(factory, "feedback") == 0
    assert client.get("/api/users/1/feedback/cluster/1").status_code == 404

    assert client.delete("/api/users/1/feedback/cluster/1").status_code == 204
    # Удаление реакции не трогает сами публикации и инфоповоды.
    assert _count(factory, "feed_items") == 7
    assert _count(factory, "content_clusters") == 3


def test_users_are_isolated(api):
    client, _, _ = api
    assert _put(client, 2, "feed_item", 105, "interested").status_code == 201

    assert client.get("/api/users/1/feedback/feed_item/105").status_code == 404
    assert client.get("/api/users/1/feedback").json() == []
    assert [item["user_id"] for item in client.get("/api/users/2/feedback").json()] == [2]

    assert _put(client, 1, "feed_item", 101, "not_interested").status_code == 201
    assert client.get("/api/users/2/feedback/feed_item/101").status_code == 404
    # Чужое удаление ничего не меняет у владельца реакции.
    assert client.delete("/api/users/2/feedback/feed_item/101").status_code == 204
    assert client.get("/api/users/1/feedback/feed_item/101").status_code == 200


def test_same_numeric_id_of_different_kinds_is_independent(api):
    client, factory, _ = api
    assert _put(client, 1, "feed_item", 101, "interested").status_code == 201
    assert _put(client, 1, "cluster", 101, "not_interested").status_code == 201
    assert _count(factory, "feedback") == 2

    feed_item = client.get("/api/users/1/feedback/feed_item/101").json()
    cluster = client.get("/api/users/1/feedback/cluster/101").json()
    assert (feed_item["kind"], feed_item["reaction"]) == ("feed_item", "interested")
    assert (cluster["kind"], cluster["reaction"]) == ("cluster", "not_interested")
    assert feed_item["id"] != cluster["id"]

    assert client.delete("/api/users/1/feedback/cluster/101").status_code == 204
    assert client.get("/api/users/1/feedback/feed_item/101").status_code == 200
    assert client.get("/api/users/1/feedback/cluster/101").status_code == 404


@pytest.mark.parametrize(
    ("kind", "unit_id"),
    [
        ("feed_item", 102),  # участник кластера нельзя оценить отдельно
        ("feed_item", 103),  # материал ещё не готов для ленты
        ("feed_item", 104),  # источник выключен у пользователя
        ("feed_item", 105),  # публикация другого пользователя
        ("feed_item", 999),  # нет такой публикации
        ("cluster", 2),  # единственный участник из выключенного источника
        ("cluster", 999),  # нет такого инфоповода
    ],
)
def test_put_requires_visible_unit(api, kind, unit_id):
    client, factory, _ = api
    response = _put(client, 1, kind, unit_id, "interested")
    assert response.status_code == 404
    assert response.json()["detail"] == "Материал недоступен."
    assert _count(factory, "feedback") == 0


def test_cluster_is_visible_when_one_member_is_visible(api):
    client, factory, _ = api
    with factory.begin() as session:
        session.execute(
            text(
                "INSERT INTO feed_items ("
                "id, source_id, external_id, title, body_text, original_url, processing_status) "
                "VALUES (108, 1, 'f108', 'Свой участник', 'Текст 108', "
                "'https://example.test/108', 'normalized')"
            )
        )
        session.execute(
            text(
                "INSERT INTO content_clusters (id, title, summary) "
                "VALUES (3, 'Смешанный', 'Сводка')"
            )
        )
        session.execute(
            text(
                "INSERT INTO cluster_items (id, cluster_id, feed_item_id, is_primary) VALUES "
                "(4, 3, 105, 1), (5, 3, 108, 0)"
            )
        )

    created = _put(client, 1, "cluster", 3, "interested")
    assert created.status_code == 201
    assert created.json()["kind"] == "cluster"
    assert created.json()["unit_id"] == 3
    # Оценка инфоповода не создаёт реакций его участникам.
    assert _count(factory, "feedback") == 1
    assert client.get("/api/users/1/feedback/feed_item/108").status_code == 404


def test_old_reaction_survives_source_disable_but_new_put_is_rejected(api):
    client, factory, _ = api
    assert _put(client, 1, "feed_item", 101, "interested").status_code == 201
    with factory.begin() as session:
        session.execute(
            text("UPDATE user_sources SET enabled = 0 WHERE user_id = 1 AND source_id = 1")
        )

    assert client.get("/api/users/1/feedback/feed_item/101").json()["reaction"] == "interested"
    assert _put(client, 1, "feed_item", 101, "not_interested").status_code == 404
    assert client.delete("/api/users/1/feedback/feed_item/101").status_code == 204
    assert client.get("/api/users/1/feedback/feed_item/101").status_code == 404


def test_list_order_filters_and_pagination(api):
    client, factory, _ = api
    first = _put(client, 1, "feed_item", 101, "interested").json()
    second = _put(client, 1, "cluster", 101, "not_interested").json()
    third = _put(client, 1, "cluster", 1, "interested").json()
    assert _count(factory, "feedback") == 3

    everything = client.get("/api/users/1/feedback").json()
    assert [item["id"] for item in everything] == [third["id"], second["id"], first["id"]]

    page = client.get("/api/users/1/feedback?limit=2").json()
    assert [item["id"] for item in page] == [third["id"], second["id"]]
    tail = client.get("/api/users/1/feedback?limit=2&offset=2").json()
    assert [item["id"] for item in tail] == [first["id"]]

    clusters = client.get("/api/users/1/feedback?kind=cluster").json()
    assert {item["unit_id"] for item in clusters} == {1, 101}
    only_items = client.get("/api/users/1/feedback?kind=feed_item").json()
    assert [item["unit_id"] for item in only_items] == [101]


def test_bulk_unit_ids_return_existing_reactions_without_pagination(api):
    client, factory, _ = api
    with factory.begin() as session:
        session.execute(
            text(
                "INSERT INTO feed_items ("
                "id, source_id, external_id, title, body_text, original_url, processing_status) "
                "VALUES (109, 1, 'f109', 'Вторая одиночная', 'Текст 109', "
                "'https://example.test/109', 'normalized')"
            )
        )
    assert _put(client, 1, "feed_item", 101, "interested").status_code == 201
    assert _put(client, 1, "feed_item", 109, "not_interested").status_code == 201
    assert _put(client, 1, "cluster", 101, "interested").status_code == 201

    response = client.get(
        "/api/users/1/feedback?kind=feed_item&unit_id=101&unit_id=109&unit_id=101&limit=1"
    )
    assert response.status_code == 200
    body = response.json()
    assert {item["unit_id"] for item in body} == {101, 109}
    assert len(body) == 2

    # Единица без реакции просто отсутствует в ответе.
    empty = client.get("/api/users/1/feedback?kind=feed_item&unit_id=105")
    assert empty.status_code == 200
    assert empty.json() == []


@pytest.mark.parametrize(
    "url",
    [
        "/api/users/0/feedback",
        "/api/users/1/feedback/post/101",
        "/api/users/1/feedback/feed_item/0",
        "/api/users/1/feedback?unit_id=101",
        "/api/users/1/feedback?kind=feed_item&unit_id=0",
        "/api/users/1/feedback?kind=post&unit_id=101",
        "/api/users/1/feedback?limit=0",
        "/api/users/1/feedback?limit=101",
        "/api/users/1/feedback?offset=-1",
    ],
)
def test_invalid_query_and_path_are_rejected(api, url):
    client, _, _ = api
    assert client.get(url).status_code == 422


def test_invalid_bulk_size_is_rejected(api):
    client, _, _ = api
    too_many = "".join(f"&unit_id={value}" for value in range(1, 102))
    assert client.get(f"/api/users/1/feedback?kind=feed_item{too_many}").status_code == 422


@pytest.mark.parametrize(
    "payload",
    [
        {"reaction": "neutral"},
        {"reaction": "useful"},  # прежнее значение: переименовано в 0013
        {"reaction": "interested", "comment": "лишнее поле"},
        {},
        {"reaction": None},
    ],
)
def test_invalid_put_payload_is_rejected(api, payload):
    client, factory, _ = api
    response = client.put("/api/users/1/feedback/feed_item/101", json=payload)
    assert response.status_code == 422
    assert _count(factory, "feedback") == 0


def test_unknown_user_returns_404(api):
    client, _, _ = api
    assert client.get("/api/users/999/feedback").status_code == 404
    assert client.get("/api/users/999/feedback/feed_item/101").status_code == 404
    assert _put(client, 999, "feed_item", 101, "interested").status_code == 404
    assert client.delete("/api/users/999/feedback/feed_item/101").status_code == 404


def test_missing_reaction_returns_404(api):
    client, _, _ = api
    response = client.get("/api/users/1/feedback/feed_item/101")
    assert response.status_code == 404
    assert response.json()["detail"] == "Реакция не найдена."


def test_openapi_contains_all_feedback_operations():
    paths = app.openapi()["paths"]
    collection = paths["/api/users/{user_id}/feedback"]
    assert set(collection) == {"get"}
    unit = paths["/api/users/{user_id}/feedback/{kind}/{unit_id}"]
    assert set(unit) == {"get", "put", "delete"}
    assert {parameter["name"] for parameter in collection["get"]["parameters"]} >= {
        "user_id",
        "kind",
        "unit_id",
        "limit",
        "offset",
    }


def _session_override(factory, engine=None, user_id: int = 1):
    """Override `get_db`; с engine удаляет реакции пользователя сразу после commit.

    Это ручная синхронизация с параллельным `DELETE`/`PUT` после фиксации, а не
    гонка потоков: порядок событий детерминирован.
    """

    def remove_after_commit(_session) -> None:
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM feedback WHERE user_id = :user_id"),
                {"user_id": user_id},
            )

    def override_db():
        session = factory()
        if engine is not None:
            event.listen(session, "after_commit", remove_after_commit)
        try:
            yield session
        finally:
            session.close()

    return override_db


@pytest.mark.parametrize("scenario", ["create", "change", "noop"])
def test_put_returns_own_snapshot_when_row_is_deleted_after_commit(api, scenario):
    """Ответ PUT не зависит от того, что строка исчезла сразу после commit."""
    client, factory, engine = api
    reaction = "not_interested" if scenario == "change" else "interested"
    before = None
    if scenario != "create":
        assert _put(client, 1, "feed_item", 101, "interested").status_code == 201
        before = client.get("/api/users/1/feedback/feed_item/101").json()

    app.dependency_overrides[get_db] = _session_override(factory, engine)
    try:
        response = _put(client, 1, "feed_item", 101, reaction)
    finally:
        app.dependency_overrides[get_db] = _session_override(factory)

    assert response.status_code == (200 if scenario != "create" else 201)
    body = response.json()
    assert (body["kind"], body["unit_id"], body["reaction"]) == ("feed_item", 101, reaction)
    if scenario == "create":
        assert body["updated_at"] is None
    elif scenario == "change":
        assert (body["id"], body["created_at"]) == (before["id"], before["created_at"])
        assert body["updated_at"] is not None
    else:
        assert (body["id"], body["created_at"]) == (before["id"], before["created_at"])
        assert body["updated_at"] is None
    # Строка удалена параллельным запросом сразу после фиксации.
    assert _count(factory, "feedback") == 0


def test_flush_error_rolls_back_and_releases_transaction(api):
    """Ошибка flush/commit откатывает транзакцию вместе с блокировкой строки."""
    client, factory, engine = api
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TRIGGER feedback_insert_guard BEFORE INSERT ON feedback "
                "BEGIN SELECT RAISE(ABORT, 'boom'); END"
            )
        )
    session = factory()
    try:
        with pytest.raises(IntegrityError):
            set_feedback(session, 1, FeedbackKind.FEED_ITEM, 101, FeedbackType.INTERESTED)
        # Транзакция и блокировка строки пользователя освобождены.
        assert not session.in_transaction()
    finally:
        session.close()
    assert _count(factory, "feedback") == 0

    with engine.begin() as connection:
        connection.execute(text("DROP TRIGGER feedback_insert_guard"))
    assert _put(client, 1, "feed_item", 101, "interested").status_code == 201
