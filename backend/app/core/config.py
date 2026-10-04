from functools import lru_cache
from pathlib import Path

from pydantic import SecretStr
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
    llm_provider: str = ""
    llm_api_key: SecretStr = SecretStr("")
    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()
