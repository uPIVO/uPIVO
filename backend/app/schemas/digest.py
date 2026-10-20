from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import DigestType
from app.schemas.feed import FeedItemListItem


class DigestCreateRequest(BaseModel):
    user_id: int = Field(ge=1, description="ID пользователя")
    type: DigestType = Field(default=DigestType.GENERAL, description="Тип дайджеста: general или topic")
    topic: str | None = Field(default=None, description="Тема (обязательна для topic дайджеста)")
    period_hours: int = Field(default=24, ge=1, le=168, description="Период выборки новостей в часах (по умолчанию 24)")


class DigestListItem(BaseModel):
    """Карточка/плашка дайджеста для вкладки дайджестов."""
    id: int
    user_id: int
    type: DigestType
    title: str = Field(description="Заголовок дайджеста, напр. 'Общий дайджест за 18.10.2026 19:30'")
    topic: str | None = None
    preview: str = Field(description="Краткое превью дайджеста (первые 200 символов)")
    period_start: datetime
    period_end: datetime
    items_count: int = Field(description="Количество публикаций в дайджесте")
    is_favorite: bool = Field(description="Сохранён ли в избранное")
    created_at: datetime = Field(description="Время создания дайджеста")

    model_config = ConfigDict(from_attributes=True)


class DigestDetailResponse(BaseModel):
    """Полный развернутый дайджест при клике на плашку."""
    id: int
    user_id: int
    type: DigestType
    title: str
    topic: str | None = None
    summary_text: str = Field(description="Полный текст сводки дайджеста")
    period_start: datetime
    period_end: datetime
    items_count: int
    is_favorite: bool
    created_at: datetime
    items: list[FeedItemListItem] = Field(default_factory=list, description="Список публикаций, вошедших в дайджест")

    model_config = ConfigDict(from_attributes=True)


class DigestToggleFavoriteResponse(BaseModel):
    id: int
    is_favorite: bool
    message: str

