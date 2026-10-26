from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import SourceType


class SourceBrief(BaseModel):
    id: int
    name: str
    type: SourceType

    model_config = ConfigDict(from_attributes=True)


class FeedItemListItem(BaseModel):
    id: int
    title: str = Field(description="Название поста с исходным регистром")
    summary: str = Field(description="Краткое изложение поста (саммари от ИИ или выдержка)")
    published_at: datetime | None = Field(default=None, description="Дата и время поста")
    source: SourceBrief = Field(
        description="Источник публикации (для кластера — основной источник)"
    )
    original_url: str = Field(description="Ссылка на оригинальный пост")
    is_cluster: bool = Field(
        default=False,
        description="Признак: объединяет ли карточка несколько публикаций одного инфоповода",
    )
    cluster_id: int | None = Field(
        default=None, description="Идентификатор кластера, если карточка является кластером"
    )
    items_count: int = Field(
        default=1, description="Количество публикаций в событии (1 для одиночного, >1 для кластера)"
    )
    related_sources: list[SourceBrief] = Field(
        default_factory=list, description="Источники всех публикаций, входящих в данный инфоповод"
    )

    model_config = ConfigDict(from_attributes=True)


class FeedItemDetail(BaseModel):
    id: int
    title: str = Field(description="Полное название поста")
    summary: str | None = Field(default=None, description="Краткое изложение поста")
    body_text: str = Field(description="Полный текст публикации")
    author: str | None = Field(default=None, description="Автор")
    published_at: datetime | None = Field(default=None, description="Дата и время поста")
    original_url: str = Field(description="Оригинальный URL")
    source: SourceBrief = Field(description="Источник публикации")
    tags: list[str] = Field(default_factory=list, description="Тематические теги/хабы")
    extracted_entities: dict[str, Any] | None = Field(
        default=None, description="Извлеченные сущности"
    )
    is_cluster: bool = Field(default=False, description="Признак кластера")
    cluster_id: int | None = Field(default=None, description="ID кластера")
    items_count: int = Field(default=1, description="Количество публикаций события")
    related_items: list[FeedItemListItem] = Field(
        default_factory=list, description="Список всех публикаций, входящих в кластер события"
    )

    model_config = ConfigDict(from_attributes=True)


class PaginatedFeedResponse(BaseModel):
    items: list[FeedItemListItem]
    total: int
    limit: int
    offset: int
