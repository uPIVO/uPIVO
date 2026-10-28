"""Register and login. No live PostgreSQL, and other user routes stay open."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth.passwords import verify_password
from app.auth.tokens import InvalidTokenError, read_user_id
from app.core.config import get_settings
from app.db.session import get_db
from app.main import app

EMAIL = "person@example.com"
PASSWORD = "correct-horse"


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
                "id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT NOT NULL UNIQUE, "
                "password_hash TEXT NOT NULL, created_at DATETIME, updated_at DATETIME)"
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


def test_register_stores_password_hash(api):
    client, factory = api
    response = client.post("/api/auth/register", json={"email": EMAIL, "password": PASSWORD})
    assert response.status_code == 201
    body = response.json()
    assert body["email"] == EMAIL
    assert body["id"] >= 1
    assert "password" not in body
    assert "password_hash" not in body
    with factory() as session:
        stored = session.scalar(
            text("SELECT password_hash FROM users WHERE email = :email"),
            {"email": EMAIL},
        )
    assert stored != PASSWORD
    assert stored.startswith("scrypt$")
    assert verify_password(PASSWORD, stored)


def test_register_duplicate_email(api):
    client, factory = api
    first = client.post("/api/auth/register", json={"email": EMAIL, "password": PASSWORD})
    again = client.post(
        "/api/auth/register",
        json={"email": "Person@Example.com", "password": "another-secret"},
    )
    assert first.status_code == 201
    assert again.status_code == 409
    assert again.json() == {"detail": "Email already registered"}
    with factory() as session:
        count = session.scalar(text("SELECT count(*) FROM users"))
    assert count == 1


def test_login_ok(api):
    client, _factory = api
    created = client.post("/api/auth/register", json={"email": EMAIL, "password": PASSWORD})
    response = client.post("/api/auth/login", json={"email": EMAIL, "password": PASSWORD})
    assert response.status_code == 200
    body = response.json()
    assert body["user_id"] == created.json()["id"]
    assert body["email"] == EMAIL
    assert body["token_type"] == "bearer"
    secret = get_settings().auth_token_secret.get_secret_value()
    assert read_user_id(body["token"], secret) == body["user_id"]
    tampered = body["token"][:-1] + ("A" if body["token"][-1] != "A" else "B")
    with pytest.raises(InvalidTokenError):
        read_user_id(tampered, secret)


def test_login_wrong_password(api):
    client, _factory = api
    client.post("/api/auth/register", json={"email": EMAIL, "password": PASSWORD})
    response = client.post(
        "/api/auth/login",
        json={"email": EMAIL, "password": "wrong-password"},
    )
    assert response.status_code == 401
    assert response.json() == {"detail": "Wrong password"}


def test_login_unknown_user(api):
    client, _factory = api
    response = client.post(
        "/api/auth/login",
        json={"email": "missing@example.com", "password": PASSWORD},
    )
    assert response.status_code == 401
    assert response.json() == {"detail": "Unknown user"}
