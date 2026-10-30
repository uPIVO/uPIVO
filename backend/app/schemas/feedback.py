"""Схемы API обратной связи по публикациям и инфоповодам.

Авторизация не входит в этот API: в пути передаётся существующий `users.id`.
Единица оценки — `feed_item` (одиночная публикация) или `cluster` (инфоповод);
числовые ID этих таблиц независимы, поэтому тип обязателен.
"""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from app.models.enums import FeedbackType


class FeedbackKind(StrEnum):
    """Тип оцениваемой единицы."""

    FEED_ITEM = "feed_item"
    CLUSTER = "cluster"


class FeedbackPutRequest(BaseModel):
    """Тело PUT: только реакция; лишние поля и неверные значения отклоняются."""

    model_config = ConfigDict(extra="forbid")

    reaction: FeedbackType


class FeedbackResponse(BaseModel):
    """Текущая реакция пользователя без чужих данных, публикаций и истории."""

    id: int
    user_id: int
    kind: FeedbackKind
    unit_id: int
    reaction: FeedbackType
    created_at: datetime
    updated_at: datetime | None
