import math
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


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


class RelevanceInput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    summary: str = Field(min_length=1, strict=True)
    interests: list[str] = Field(min_length=1, strict=True)

    @field_validator("summary", mode="before")
    @classmethod
    def require_nonblank_summary(cls, value: Any) -> Any:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("Выжимка должна быть непустым текстом.")
        return value

    @field_validator("interests", mode="before")
    @classmethod
    def require_nonblank_interests(cls, value: Any) -> Any:
        if not isinstance(value, list) or not value:
            raise ValueError("Список интересов должен быть непустым.")
        if any(not isinstance(item, str) or not item.strip() for item in value):
            raise ValueError("Каждый интерес должен быть непустым текстом.")
        return value


class RelevanceUsage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    input_tokens: int | None = Field(default=None, ge=0, strict=True)
    output_tokens: int | None = Field(default=None, ge=0, strict=True)


class RelevanceResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    relevance: float = Field(ge=0, le=1, strict=True, allow_inf_nan=False)
    fingerprint: str = Field(pattern=r"^v1:[0-9a-f]{64}$")
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    usage: RelevanceUsage | None = None

    @field_validator("relevance", mode="before")
    @classmethod
    def require_finite_probability(cls, value: Any) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("Оценка релевантности должна быть числом.")
        try:
            probability = float(value)
        except (OverflowError, ValueError):
            raise ValueError("Оценка релевантности должна быть конечной.") from None
        if not math.isfinite(probability) or not 0.0 <= probability <= 1.0:
            raise ValueError("Оценка релевантности вне диапазона от 0 до 1.")
        return probability


class RelevanceRun(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    result: RelevanceResult
    reused: bool
    elapsed_seconds: float = Field(ge=0, allow_inf_nan=False)


class SummarizationRun(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    result: SummaryResult
    reused: bool
    elapsed_seconds: float = Field(ge=0)
