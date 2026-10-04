from unittest.mock import Mock

from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from app.db.session import get_db
from app.main import app


def test_health_without_external_services():
    with TestClient(app) as client:
        assert client.get("/health").json() == {"status": "ok"}
        assert client.get("/openapi.json").status_code == 200


def test_database_health_success():
    session = Mock()
    app.dependency_overrides[get_db] = lambda: session
    try:
        with TestClient(app) as client:
            assert client.get("/health/db").json() == {"status": "ok"}
        assert str(session.execute.call_args.args[0]) == "SELECT 1"
    finally:
        app.dependency_overrides.clear()


def test_database_health_unavailable_does_not_expose_error():
    session = Mock()
    session.execute.side_effect = OperationalError("SELECT 1", {}, Exception("secret"))
    app.dependency_overrides[get_db] = lambda: session
    try:
        with TestClient(app) as client:
            response = client.get("/health/db")
        assert response.status_code == 503
        assert response.json() == {"detail": "Database unavailable"}
    finally:
        app.dependency_overrides.clear()
