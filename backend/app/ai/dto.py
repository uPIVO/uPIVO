import math
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)


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


class ImportanceInput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    summary: str = Field(min_length=1, strict=True)

    @field_validator("summary", mode="before")
    @classmethod
    def require_nonblank_summary(cls, value: Any) -> Any:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("Выжимка должна быть непустым текстом.")
        return value


class ImportanceUsage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    input_tokens: int | None = Field(default=None, ge=0, strict=True)
    output_tokens: int | None = Field(default=None, ge=0, strict=True)


class ImportanceWeights(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    scale: float = Field(ge=0, strict=True, allow_inf_nan=False)
    significance: float = Field(ge=0, strict=True, allow_inf_nan=False)
    reaction: float = Field(ge=0, strict=True, allow_inf_nan=False)

    @field_validator("scale", "significance", "reaction", mode="before")
    @classmethod
    def require_finite_nonnegative_weight(cls, value: Any, info: ValidationInfo) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("Вес оценки должен быть числом.")
        try:
            weight = float(value)
        except (OverflowError, ValueError):
            raise ValueError("Вес оценки должен быть конечным.") from None
        if not math.isfinite(weight) or weight < 0:
            raise ValueError("Вес оценки должен быть конечным неотрицательным числом.")
        return weight


class ImportanceComponents(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    scale_score: float = Field(ge=0, le=4, strict=True, allow_inf_nan=False)
    significance_score: float = Field(ge=0, le=4, strict=True, allow_inf_nan=False)
    reaction_score: float = Field(ge=0, le=2, strict=True, allow_inf_nan=False)
    fingerprint: str = Field(pattern=r"^v1:[0-9a-f]{64}$")
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    usage: ImportanceUsage | None = None

    @field_validator("scale_score", "significance_score", "reaction_score", mode="before")
    @classmethod
    def require_finite_score(cls, value: Any, info: ValidationInfo) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("Оценка общей важности должна быть числом.")
        try:
            score = float(value)
        except (OverflowError, ValueError):
            raise ValueError("Оценка общей важности должна быть конечной.") from None
        maximum = 2.0 if info.field_name == "reaction_score" else 4.0
        if not math.isfinite(score) or not 0.0 <= score <= maximum:
            raise ValueError("Оценка общей важности вне допустимого диапазона.")
        return score


class ImportanceResult(ImportanceComponents):
    total_score: float = Field(ge=0, le=8, strict=True, allow_inf_nan=False)
    weights: ImportanceWeights
    aggregation_version: str = Field(min_length=1)

    @field_validator("total_score", mode="before")
    @classmethod
    def require_finite_total(cls, value: Any) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("Итоговая оценка важности должна быть числом.")
        try:
            total = float(value)
        except (OverflowError, ValueError):
            raise ValueError("Итоговая оценка важности должна быть конечной.") from None
        if not math.isfinite(total) or not 0.0 <= total <= 8.0:
            raise ValueError("Итоговая оценка важности вне диапазона от 0 до 8.")
        return total

    @model_validator(mode="after")
    def require_consistent_total(self) -> "ImportanceResult":
        expected = (
            self.weights.scale * self.scale_score
            + self.weights.significance * self.significance_score
            + self.weights.reaction * self.reaction_score
        )
        if not math.isclose(self.total_score, expected, rel_tol=0.0, abs_tol=1e-12):
            raise ValueError("Итоговая оценка не соответствует весам компонентов.")
        return self


class ImportanceRun(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    result: ImportanceResult
    reused: bool
    elapsed_seconds: float = Field(ge=0, allow_inf_nan=False)


class SummarizationRun(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    result: SummaryResult
    reused: bool
    elapsed_seconds: float = Field(ge=0)
