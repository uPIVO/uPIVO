from datetime import UTC, datetime, timedelta

import pytest

from app.services.ranking import rank_content_unit

NOW = datetime(2026, 10, 20, tzinfo=UTC)


def score(*, relevance: float = 0.5, importance: float = 5, age_hours: int = 1) -> float:
    return rank_content_unit(
        relevance_score=relevance,
        importance_score=importance,
        published_at=NOW - timedelta(hours=age_hours),
        now=NOW,
    )


def test_each_signal_increases_rank_independently():
    assert score(importance=6) > score(importance=5)
    assert score(relevance=0.6) > score(relevance=0.5)
    assert score(age_hours=1) > score(age_hours=48)


def test_rank_is_bounded_and_rejects_invalid_scores():
    assert 0 <= score(relevance=0, importance=0, age_hours=1000) <= 1
    assert score(relevance=1, importance=10, age_hours=0) == 1
    with pytest.raises(ValueError, match="importance_score"):
        score(importance=11)
