import pytest
from pydantic import SecretStr, ValidationError

from app.connectors.telegram.credentials import (
    TelegramApiCredentials,
    TelegramSessionCredentials,
)
from app.connectors.telegram.errors import TelegramConfigurationError
from app.core.config import Settings


def settings(*, api_id="12345", api_hash="fixture-api-hash", session=""):
    return Settings(
        _env_file=None,
        telegram_api_id=api_id,
        telegram_api_hash=SecretStr(api_hash),
        telegram_session=SecretStr(session),
    )


def test_api_credentials_do_not_require_a_session():
    credentials = TelegramApiCredentials.from_settings(settings())

    assert credentials.api_id == 12345
    assert credentials.api_hash.get_secret_value() == "fixture-api-hash"
    assert not hasattr(credentials, "session")


def test_session_credentials_require_a_nonempty_session():
    with pytest.raises(TelegramConfigurationError, match="TELEGRAM_SESSION"):
        TelegramSessionCredentials.from_settings(settings())

    credentials = TelegramSessionCredentials.from_settings(settings(session="fixture-session"))
    assert credentials.api_id == 12345
    assert credentials.session.get_secret_value() == "fixture-session"


def test_pydantic_uses_positive_ids_and_nonempty_secret_fields():
    with pytest.raises(ValidationError):
        TelegramApiCredentials.model_validate({"api_id": 0, "api_hash": SecretStr("hash")})
    with pytest.raises(ValidationError):
        TelegramApiCredentials.model_validate({"api_id": 12345, "api_hash": SecretStr("")})
    with pytest.raises(ValidationError):
        TelegramSessionCredentials.model_validate(
            {
                "api_id": 12345,
                "api_hash": SecretStr("hash"),
                "session": SecretStr(""),
            }
        )


def test_settings_conversion_hides_invalid_secret_values():
    api_hash_marker = "fixture-hash-must-not-leak"
    session_marker = "fixture-session-must-not-leak"

    with pytest.raises(TelegramConfigurationError) as caught:
        TelegramSessionCredentials.from_settings(
            settings(api_id="not-an-integer", api_hash=api_hash_marker, session=session_marker)
        )

    assert api_hash_marker not in str(caught.value)
    assert session_marker not in str(caught.value)


def test_settings_remain_lazy_without_telegram_environment(monkeypatch):
    for name in ("TELEGRAM_API_ID", "TELEGRAM_API_HASH", "TELEGRAM_SESSION"):
        monkeypatch.delenv(name, raising=False)

    current_settings = Settings(_env_file=None)

    assert current_settings.telegram_api_id == ""
    assert current_settings.telegram_api_hash.get_secret_value() == ""
    assert current_settings.telegram_session.get_secret_value() == ""
