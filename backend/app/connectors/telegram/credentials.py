from typing import ClassVar, Self

from pydantic import BaseModel, Field, PositiveInt, SecretStr, ValidationError

from app.connectors.telegram.errors import TelegramConfigurationError
from app.core.config import Settings


class TelegramApiCredentials(BaseModel):
    api_id: PositiveInt
    api_hash: SecretStr = Field(min_length=1)

    _settings_error_message: ClassVar[str] = (
        "Укажите корректные TELEGRAM_API_ID и TELEGRAM_API_HASH в корневом .env."
    )

    @classmethod
    def _settings_values(cls, settings: Settings) -> dict[str, object]:
        return {
            "api_id": settings.telegram_api_id,
            "api_hash": settings.telegram_api_hash,
        }

    @classmethod
    def from_settings(cls, settings: Settings) -> Self:
        try:
            return cls.model_validate(cls._settings_values(settings))
        except ValidationError:
            raise TelegramConfigurationError(cls._settings_error_message) from None


class TelegramSessionCredentials(TelegramApiCredentials):
    session: SecretStr = Field(min_length=1)

    _settings_error_message: ClassVar[str] = (
        "Укажите корректные TELEGRAM_API_ID, TELEGRAM_API_HASH и TELEGRAM_SESSION в корневом .env."
    )

    @classmethod
    def _settings_values(cls, settings: Settings) -> dict[str, object]:
        return {
            **super()._settings_values(settings),
            "session": settings.telegram_session,
        }
