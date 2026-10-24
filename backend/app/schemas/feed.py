from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import SourceType


class SourceBrief(BaseModel):
    id: int
    name: str
    type: SourceType

    model_config = ConfigDict(from_attributes=True)


class RelatedFeedItem(BaseModel):
    id: int
    title: str = Field(description="Название связанной публикации")
    source: SourceBrief = Field(description="Источник связанной публикации")
    original_url: str = Field(description="Ссылка на оригинальный пост")
    published_at: datetime | None = Field(default=None, description="Дата публикации")

    model_config = ConfigDict(from_attributes=True)


class FeedItemListItem(BaseModel):
    id: int
    title: str = Field(description="Название поста заглавными буквами")
    summary: str = Field(description="Краткое изложение поста (саммари от ИИ или выдержка)")
    published_at: datetime | None = Field(default=None, description="Дата и время поста")
    source: SourceBrief = Field(description="Основной источник публикации")
    original_url: str = Field(description="Ссылка на оригинальный пост")

    # Поддержка кластеров (несколько публикаций одного события как одна карточка)
    cluster_id: int | None = Field(default=None, description="ID кластера события (если несколько публикаций объединены)")
    is_cluster: bool = Field(default=False, description="Является ли карточка объединением нескольких публикаций")
    publications_count: int = Field(default=1, description="Количество публикаций по данному событию")
    sources: list[SourceBrief] = Field(default_factory=list, description="Все источники, осветившие данное событие")
    related_items: list[RelatedFeedItem] = Field(default_factory=list, description="Связанные публикации по этому же событию")

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
    extracted_entities: dict[str, Any] | None = Field(default=None, description="Извлеченные сущности")

    # Поддержка кластеров
    cluster_id: int | None = Field(default=None, description="ID кластера события")
    related_publications: list[RelatedFeedItem] = Field(default_factory=list, description="Другие публикации этого же события")

    model_config = ConfigDict(from_attributes=True)


class PaginatedFeedResponse(BaseModel):
    items: list[FeedItemListItem]
    total: int
    limit: int
    offset: int


