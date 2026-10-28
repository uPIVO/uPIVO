"""Request and response models for per-user sources and topics.

Callers pass an existing users.id. The HTTP routes require that user's bearer token.
"""

from datetime import datetime

from pydantic import BaseModel, Field

from app.models.enums import SourceType


class SourceAttachRequest(BaseModel):
    type: SourceType
    url: str = Field(min_length=1, max_length=4096)
    name: str | None = Field(default=None, max_length=255)


class SourceEnabledRequest(BaseModel):
    enabled: bool


class SourceLinkResponse(BaseModel):
    id: int
    user_id: int
    source_id: int
    enabled: bool
    type: SourceType
    name: str
    canonical_url: str
    external_id: str | None
    created_at: datetime


class TopicCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str | None = None
    parent_id: int | None = Field(default=None, ge=1)


class TopicUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None


class TopicItemRequest(BaseModel):
    feed_item_id: int = Field(ge=1)


class TopicResponse(BaseModel):
    id: int
    user_id: int
    name: str
    description: str | None
    parent_id: int | None
    feed_item_ids: list[int]
    created_at: datetime
    updated_at: datetime | None
