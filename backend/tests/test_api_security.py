"""Real signed authentication and ownership boundaries across integrated routers."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth.tokens import issue_token
from app.core.config import get_settings
from app.db.session import get_db
from app.main import app
from tests.auth_helpers import auth_headers

USER_ROUTES = [
    ("GET", "/api/users/2/sources", None),
    ("POST", "/api/users/2/sources", {"type": "habr", "url": "https://habr.com/ru/all/"}),
    ("PATCH", "/api/users/2/sources/1", {"enabled": False}),
    ("DELETE", "/api/users/2/sources/1", None),
    ("GET", "/api/users/2/topics", None),
    ("POST", "/api/users/2/topics", {"name": "Test"}),
    ("PATCH", "/api/users/2/topics/1", {"name": "Test"}),
    ("POST", "/api/users/2/topics/1/items", {"feed_item_id": 1}),
    ("DELETE", "/api/users/2/topics/1/items/1", None),
    ("GET", "/api/users/2/interests", None),
    ("POST", "/api/users/2/interests", {"text": "Python"}),
    ("PATCH", "/api/users/2/interests/1", {"text": "Python"}),
    ("DELETE", "/api/users/2/interests/1", None),
    ("GET", "/api/users/2/feedback", None),
    ("GET", "/api/users/2/feedback/feed_item/1", None),
    ("PUT", "/api/users/2/feedback/feed_item/1", {"reaction": "interested"}),
    ("DELETE", "/api/users/2/feedback/feed_item/1", None),
    ("GET", "/api/feed?user_id=2", None),
    ("GET", "/api/feed/1?user_id=2", None),
    ("GET", "/api/digests?user_id=2", None),
    ("GET", "/api/digests/1?user_id=2", None),
    ("PATCH", "/api/digests/1/favorite?user_id=2", None),
    ("POST", "/api/digests/generate", {"user_id": 2}),
]
OTHER_ROUTES = [
    ("GET", "/api/favorites", None),
    ("POST", "/api/favorites", {"feed_item_id": 1}),
    ("PATCH", "/api/favorites/1", {"note": "test"}),
    ("DELETE", "/api/favorites/1", None),
    ("DELETE", "/api/favorites?target_type=feed_item&target_id=1", None),
    ("GET", "/api/habr/preview", None),
    ("GET", "/api/habr/saved", None),
    ("POST", "/api/habr/sync", {}),
]


@pytest.fixture
def client():
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT, password_hash TEXT, "
                "created_at DATETIME, updated_at DATETIME)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO users (id,email,password_hash) VALUES "
                "(1,'first@example.test','unused'), (2,'second@example.test','unused')"
            )
        )
    factory = sessionmaker(bind=engine)

    def session():
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = session
    try:
        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


@pytest.mark.parametrize("method,url,payload", USER_ROUTES + OTHER_ROUTES)
@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"X-User-Id": "2"},
        {"Authorization": "Bearer 2"},
        {"Authorization": "Bearer e30.eyJzdWIiOjJ9.fake"},
    ],
)
def test_private_routes_reject_identity_shortcuts(client, method, url, payload, headers):
    response = client.request(method, url, json=payload, headers=headers)
    assert response.status_code == 401, response.text
    assert response.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize("method,url,payload", USER_ROUTES)
def test_other_user_cannot_read_or_modify(client, method, url, payload):
    response = client.request(method, url, json=payload, headers=auth_headers(1))
    assert response.status_code == 403, response.text


@pytest.mark.parametrize("case", ["expired", "wrong_secret", "deleted_user"])
def test_invalid_signed_tokens(client, case):
    secret = get_settings().auth_token_secret.get_secret_value()
    token = issue_token(
        user_id=999 if case == "deleted_user" else 1,
        secret="other-secret" if case == "wrong_secret" else secret,
        ttl_seconds=1,
        now=0 if case == "expired" else None,
    )
    assert (
        client.get("/api/favorites", headers={"Authorization": f"Bearer {token}"}).status_code
        == 401
    )


def test_auth_fails_closed_without_secret(client, monkeypatch):
    headers = auth_headers(1)
    monkeypatch.setenv("AUTH_TOKEN_SECRET", "")
    get_settings.cache_clear()
    assert client.get("/api/favorites", headers=headers).status_code == 503


def test_non_ascii_token_is_rejected_without_server_error():
    from app.auth.tokens import InvalidTokenError, read_user_id

    with pytest.raises(InvalidTokenError):
        read_user_id("не-токен.signature", "test-secret")
