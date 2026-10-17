from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.ai.analysis_metadata import (
    FeedItemAIMetadata,
    ImportanceResultHistory,
    RelevanceResultHistory,
    StaleImportanceCandidate,
    StaleRelevanceCandidate,
    StaleSummaryCandidate,
    StoredImportanceResult,
    StoredRelevanceResult,
    StoredSummaryResult,
    SummaryResultHistory,
    UserItemScoreAIMetadata,
    content_input_fingerprint,
    interests_input_fingerprint,
)
from app.ai.dto import (
    ContentUnitInput,
    ImportanceResult,
    ImportanceWeights,
    RelevanceResult,
    RelevanceUsage,
    SummaryResult,
    SummaryUsage,
)


def _summary_result(summary: str = "Короткая выжимка") -> SummaryResult:
    return SummaryResult(
        summary=summary,
        fingerprint=f"v1:{'a' * 64}",
        provider="openrouter",
        preset="@preset/test",
        usage=SummaryUsage(prompt_tokens=12, completion_tokens=4, total_tokens=16),
        cost_usd=Decimal("0.0012"),
    )


def _importance_result() -> ImportanceResult:
    return ImportanceResult(
        scale_score=2.0,
        significance_score=1.0,
        reaction_score=1.0,
        fingerprint=f"v1:{'b' * 64}",
        provider="typesafe",
        model="jev-test",
        usage=None,
        total_score=3.0,
        weights=ImportanceWeights(scale=0.5, significance=1.0, reaction=1.0),
        aggregation_version="content-importance-weighted-v1",
    )


def _relevance_result() -> RelevanceResult:
    return RelevanceResult(
        relevance=0.75,
        fingerprint=f"v1:{'c' * 64}",
        provider="typesafe",
        model="jev-test",
        usage=RelevanceUsage(input_tokens=10, output_tokens=1),
    )


def _unit(*, source_name: str = "Источник") -> ContentUnitInput:
    return ContentUnitInput(
        kind="feed_item",
        id=7,
        feed_items=[
            {
                "feed_item_id": 7,
                "body_text": "Нормализованное содержимое",
                "original_url": "https://example.test/item",
                "source_name": source_name,
            }
        ],
    )


def test_content_input_fingerprint_is_stable_and_includes_source_name() -> None:
    first = content_input_fingerprint(_unit())
    same = content_input_fingerprint(_unit())
    changed_source = content_input_fingerprint(_unit(source_name="Другой источник"))

    assert first.startswith("input-v1:")
    assert first == same
    assert first != changed_source


def test_feed_item_metadata_round_trips_last_good_and_one_stale_candidate() -> None:
    last_good = StoredSummaryResult(
        status="stale",
        input_fingerprint=f"input-v1:{'1' * 64}",
        result=_summary_result(),
    )
    stale_candidate = StaleSummaryCandidate(
        input_fingerprint=f"input-v1:{'2' * 64}",
        result=_summary_result("Кандидат для устаревшего снимка"),
    )
    importance = StoredImportanceResult(
        status="stale",
        input_fingerprint=f"input-v1:{'1' * 64}",
        summary_fingerprint=f"v1:{'a' * 64}",
        result=_importance_result(),
    )
    stale_importance = StaleImportanceCandidate(
        input_fingerprint=f"input-v1:{'2' * 64}",
        summary_fingerprint=f"v1:{'d' * 64}",
        result=_importance_result(),
    )
    metadata = FeedItemAIMetadata(
        summary=SummaryResultHistory(last_good=last_good, stale_candidate=stale_candidate),
        importance=ImportanceResultHistory(last_good=importance, stale_candidate=stale_importance),
    )

    stored = metadata.model_dump(mode="json", exclude_none=True)
    restored = FeedItemAIMetadata.model_validate(stored)

    assert restored == metadata
    assert restored.summary.last_good.result.usage.total_tokens == 16
    assert restored.summary.last_good.result.cost_usd == Decimal("0.0012")
    assert restored.summary.stale_candidate.status == "stale"
    assert restored.importance.last_good.result.total_score == 3.0
    assert "api_key" not in str(stored).lower()
    assert "interests" not in str(stored).lower()


def test_relevance_metadata_keeps_only_fingerprints_not_raw_interests() -> None:
    interests = ["личный интерес", "внутренняя тема"]
    interests_fingerprint = interests_input_fingerprint(interests)
    last_good = StoredRelevanceResult(
        status="current",
        input_fingerprint=_relevance_result().fingerprint,
        source_input_fingerprint=f"input-v1:{'1' * 64}",
        summary_fingerprint=f"v1:{'a' * 64}",
        interests_fingerprint=interests_fingerprint,
        result=_relevance_result(),
    )
    stale_candidate = StaleRelevanceCandidate(
        input_fingerprint=f"v1:{'e' * 64}",
        source_input_fingerprint=f"input-v1:{'2' * 64}",
        summary_fingerprint=f"v1:{'d' * 64}",
        interests_fingerprint=interests_fingerprint,
        result=_relevance_result(),
    )
    metadata = UserItemScoreAIMetadata(
        relevance=RelevanceResultHistory(last_good=last_good, stale_candidate=stale_candidate)
    )

    stored = metadata.model_dump(mode="json", exclude_none=True)
    restored = UserItemScoreAIMetadata.model_validate(stored)

    assert restored == metadata
    assert interests_fingerprint.startswith("interests-v1:")
    assert all(value not in str(stored) for value in interests)
    with pytest.raises(ValidationError):
        UserItemScoreAIMetadata.model_validate({**stored, "interests": interests})
