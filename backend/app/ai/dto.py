from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ContentUnitPublication(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    feed_item_id: int = Field(gt=0, strict=True)
    body_text: str = Field(min_length=1)
    original_url: str = Field(min_length=1)
    title: str | None = None
    published_at: datetime | None = None
    source_name: str | None = None
    extracted_entities: dict[str, Any] | None = None
    is_primary: bool | None = None


class ContentUnitInput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    id: int = Field(gt=0, strict=True)
    feed_items: list[ContentUnitPublication] = Field(min_length=1)
    title: str | None = None


class SummaryUsage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    prompt_tokens: int | None = Field(default=None, ge=0)
    completion_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)


class SummaryResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    summary: str = Field(min_length=1)
    fingerprint: str = Field(pattern=r"^v1:[0-9a-f]{64}$")
    provider: str = Field(min_length=1)
    preset: str = Field(min_length=1)
    usage: SummaryUsage | None = None
    cost_usd: Decimal | None = Field(default=None, ge=0)


class SummarizationRun(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    result: SummaryResult
    reused: bool
    elapsed_seconds: float = Field(ge=0)
