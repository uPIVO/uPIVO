from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT_ENV = Path(__file__).resolve().parents[3] / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT_ENV, env_file_encoding="utf-8", extra="ignore")
    app_env: str = "development"
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    database_url: SecretStr = SecretStr("postgresql+psycopg://upivo:upivo@localhost:5432/upivo")
    redis_url: SecretStr = SecretStr("redis://localhost:6379/0")
    telegram_api_id: str = ""
    telegram_api_hash: SecretStr = SecretStr("")
    telegram_session: SecretStr = SecretStr("")
    llm_provider: str = "openrouter"
    llm_api_key: SecretStr = SecretStr("")
    llm_base_url: str = "https://openrouter.ai/api/v1"
    llm_model: str = "@preset/deep-seek-v4-flash-via-direct-deep-seek-provider"
    llm_timeout_seconds: float = Field(default=60.0, gt=0, le=300)
    typesafe_api_key: SecretStr = SecretStr("")
    typesafe_base_url: str = "https://api.typesafe.ai"
    typesafe_model: str = "jev-1.14.0"
    typesafe_timeout_seconds: float = Field(default=60.0, gt=0, le=300)
    log_level: str = "INFO"
    auth_token_secret: SecretStr = SecretStr("dev-only-change-me")
    auth_token_ttl_seconds: int = Field(default=7 * 24 * 60 * 60, gt=0)


@lru_cache
def get_settings() -> Settings:
    return Settings()
