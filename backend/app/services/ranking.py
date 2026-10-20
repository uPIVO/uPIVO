"""Provisional deterministic rank for a single content unit.

This pure function does not write user scores or select feed cards.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime

RELEVANCE_WEIGHT = 0.5
IMPORTANCE_WEIGHT = 0.3
FRESHNESS_WEIGHT = 0.2
FRESHNESS_HALF_LIFE_HOURS = 24.0


def rank_content_unit(
    *,
    relevance_score: float,
    importance_score: float,
    published_at: datetime,
    now: datetime,
) -> float:
    """Return an MVP score in [0, 1] from relevance, importance and age."""
    if not math.isfinite(relevance_score) or not 0 <= relevance_score <= 1:
        raise ValueError("relevance_score must be in [0, 1]")
    if not math.isfinite(importance_score) or not 0 <= importance_score <= 10:
        raise ValueError("importance_score must be in [0, 10]")
    published = _utc(published_at)
    current = _utc(now)
    age_hours = max(0.0, (current - published).total_seconds() / 3600)
    freshness = 2 ** (-age_hours / FRESHNESS_HALF_LIFE_HOURS)
    return (
        RELEVANCE_WEIGHT * relevance_score
        + IMPORTANCE_WEIGHT * (importance_score / 10)
        + FRESHNESS_WEIGHT * freshness
    )


def _utc(value: datetime) -> datetime:
    return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
