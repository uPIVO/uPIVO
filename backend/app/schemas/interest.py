"""Схемы API интересов пользователя.

Авторизация не входит в этот API: в пути передаётся существующий `users.id`.
Интерес — один свободный непустой текст, без отдельного названия и описания.
"""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator


class _InterestTextRequest(BaseModel):
    text: str = Field(min_length=1)

    @field_validator("text", mode="before")
    @classmethod
    def require_nonblank_text(cls, value: Any) -> Any:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("Текст интереса должен быть непустым.")
        return value


class InterestCreateRequest(_InterestTextRequest):
    pass


class InterestUpdateRequest(_InterestTextRequest):
    pass


class InterestResponse(BaseModel):
    id: int
    user_id: int
    text: str
    created_at: datetime
    updated_at: datetime | None
