"""Isolate authentication configuration from developer secrets."""

import pytest

from app.core.config import get_settings


@pytest.fixture(autouse=True)
def test_auth_secret(monkeypatch):
    monkeypatch.setenv("AUTH_TOKEN_SECRET", "sprint-three-test-secret-not-for-deployment")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
