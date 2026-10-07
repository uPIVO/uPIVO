from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# --- DTO для внутреннего API Хабра (kek/v2) ---

class HabrAuthorPayload(BaseModel):
    alias: str
    fullname: str | None = None


class HabrHubPayload(BaseModel):
    title: str
    alias: str | None = None


class HabrTagPayload(BaseModel):
    title_html: str = Field(alias="titleHtml")


class HabrStatisticsPayload(BaseModel):
    score: int = 0
    votes_count: int = Field(default=0, alias="votesCount")
    reading_count: int = Field(default=0, alias="readingCount")


class HabrArticlePayload(BaseModel):
    id: str
    time_published: datetime = Field(alias="timePublished")
    title_html: str = Field(alias="titleHtml")
    text_html: str | None = Field(default=None, alias="textHtml")
    lead_data: dict[str, Any] | None = Field(default=None, alias="leadData")
    author: HabrAuthorPayload | None = None
    statistics: HabrStatisticsPayload | None = None
    hubs: list[HabrHubPayload] = Field(default_factory=list)
    tags: list[HabrTagPayload] = Field(default_factory=list)
    reading_time: int = Field(default=0, alias="readingTime")
    post_type: str = Field(default="article", alias="postType")


# --- Схемы для базы данных и эндпоинтов FastAPI ---

class HabrPostBase(BaseModel):
    habr_id: str
    title: str
    content: str
    author: str
    published_at: datetime
    hubs: list[str]
    tags: list[str]
    score: int
    reading_time: int
    original_url: str


class HabrPostCreate(HabrPostBase):
    pass


class HabrPostResponse(HabrPostBase):
    id: int
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class HabrSyncRequest(BaseModel):
    pages: int = Field(default=1, ge=1, le=20, description="Количество страниц для сбора")
    sort: str = Field(default="date", description="Сортировка: 'date' (новые) или 'rating'")


class HabrSyncResponse(BaseModel):
    fetched: int
    saved: int
    skipped: int
