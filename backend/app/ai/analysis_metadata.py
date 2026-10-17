from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.ai.dto import ContentUnitInput, ImportanceResult, RelevanceResult, SummaryResult

_INPUT_FINGERPRINT = r"^input-v1:[0-9a-f]{64}$"
_INTERESTS_FINGERPRINT = r"^interests-v1:[0-9a-f]{64}$"


class StoredSummaryResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["current", "stale"]
    input_fingerprint: str = Field(pattern=_INPUT_FINGERPRINT)
    result: SummaryResult


class StaleSummaryCandidate(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["stale"] = "stale"
    input_fingerprint: str = Field(pattern=_INPUT_FINGERPRINT)
    result: SummaryResult


class SummaryResultHistory(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    last_good: StoredSummaryResult | None = None
    stale_candidate: StaleSummaryCandidate | None = None


class StoredImportanceResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["current", "stale"]
    input_fingerprint: str = Field(pattern=_INPUT_FINGERPRINT)
    summary_fingerprint: str = Field(pattern=r"^v1:[0-9a-f]{64}$")
    result: ImportanceResult


class StaleImportanceCandidate(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["stale"] = "stale"
    input_fingerprint: str = Field(pattern=_INPUT_FINGERPRINT)
    summary_fingerprint: str = Field(pattern=r"^v1:[0-9a-f]{64}$")
    result: ImportanceResult


class ImportanceResultHistory(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    last_good: StoredImportanceResult | None = None
    stale_candidate: StaleImportanceCandidate | None = None


class FeedItemAIMetadata(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = 1
    summary: SummaryResultHistory = Field(default_factory=SummaryResultHistory)
    importance: ImportanceResultHistory = Field(default_factory=ImportanceResultHistory)


class StoredRelevanceResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["current", "stale"]
    input_fingerprint: str = Field(pattern=r"^v1:[0-9a-f]{64}$")
    source_input_fingerprint: str = Field(pattern=_INPUT_FINGERPRINT)
    summary_fingerprint: str = Field(pattern=r"^v1:[0-9a-f]{64}$")
    interests_fingerprint: str = Field(pattern=_INTERESTS_FINGERPRINT)
    result: RelevanceResult


class StaleRelevanceCandidate(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["stale"] = "stale"
    input_fingerprint: str = Field(pattern=r"^v1:[0-9a-f]{64}$")
    source_input_fingerprint: str = Field(pattern=_INPUT_FINGERPRINT)
    summary_fingerprint: str = Field(pattern=r"^v1:[0-9a-f]{64}$")
    interests_fingerprint: str = Field(pattern=_INTERESTS_FINGERPRINT)
    result: RelevanceResult


class RelevanceResultHistory(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    last_good: StoredRelevanceResult | None = None
    stale_candidate: StaleRelevanceCandidate | None = None


class UserItemScoreAIMetadata(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = 1
    relevance: RelevanceResultHistory = Field(default_factory=RelevanceResultHistory)


def content_input_fingerprint(unit: ContentUnitInput) -> str:
    encoded = json.dumps(
        unit.model_dump(mode="json", exclude_none=True),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    return f"input-v1:{digest}"


def interests_input_fingerprint(interests: list[str]) -> str:
    encoded = json.dumps(interests, ensure_ascii=False, separators=(",", ":"))
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    return f"interests-v1:{digest}"
