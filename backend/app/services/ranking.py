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


DEFAULT_RELEVANCE_SCORE = 0.5
DEFAULT_IMPORTANCE_SCORE = 5.0


def calculate_card_rank(
    *,
    is_cluster: bool,
    cluster_id: int | None,
    primary_item: object,
    feed_items: list[tuple[object, object, bool]],
    cluster_importance: float | None,
    user_scores_by_feed: dict[int, float],
    user_scores_by_cluster: dict[int, float],
    now: datetime,
) -> float:
    """
    Calculate rank score for a feed/digest card:
    - Resolves relevance_score from UserItemScore (cluster or primary/max items, fallback 0.5)
    - Resolves importance_score from cluster or primary item (fallback 5.0, clamped [0, 10])
    - Resolves published_at from primary item (or collected_at or now)
    """
    # 1. Relevance
    if is_cluster and cluster_id and cluster_id in user_scores_by_cluster:
        relevance = user_scores_by_cluster[cluster_id]
    else:
        # Check feed items
        feed_relevances = [
            user_scores_by_feed[item.id]
            for item, _, _ in feed_items
            if getattr(item, "id", None) in user_scores_by_feed
        ]
        if feed_relevances:
            relevance = max(feed_relevances)
        else:
            relevance = DEFAULT_RELEVANCE_SCORE

    relevance = max(0.0, min(1.0, float(relevance)))

    # 2. Importance
    raw_importance = None
    if is_cluster and cluster_importance is not None:
        raw_importance = cluster_importance
    elif getattr(primary_item, "importance_score", None) is not None:
        raw_importance = primary_item.importance_score

    if raw_importance is None:
        importance = DEFAULT_IMPORTANCE_SCORE
    else:
        importance = max(0.0, min(10.0, float(raw_importance)))

    # 3. Publication time
    pub_at = (
        getattr(primary_item, "published_at", None)
        or getattr(primary_item, "collected_at", None)
        or now
    )

    return rank_content_unit(
        relevance_score=relevance,
        importance_score=importance,
        published_at=pub_at,
        now=now,
    )
