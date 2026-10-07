from datetime import datetime
from typing import Any
from pydantic import BaseModel, ConfigDict, Field


class HabrPostBase(BaseModel):
    habr_id: str
    title: str
    content: str
    author: str
    published_at: datetime | str
    hubs: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    score: int = 0
    reading_time: int = 0
    original_url: str


class HabrSyncRequest(BaseModel):
    pages: int = Field(default=1, ge=1, le=10, description="Количество страниц для сбора и сохранения")


class HabrSyncResponse(BaseModel):
    fetched: int
    saved: int
    skipped: int


class FeedItemResponse(BaseModel):
    id: int
    source: str
    external_id: str
    title: str
    content: str
    author: str | None = None
    url: str | None = None
    published_at: datetime | None = None
    raw_data: dict[str, Any] | None = None

    model_config = ConfigDict(from_attributes=True)