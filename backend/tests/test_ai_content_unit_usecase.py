from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from app.ai.repository import FeedItemInClusterError, SaveDisposition
from app.services.analyze_feed_item import (
    AnalysisOperation,
    AnalysisRequestError,
    ContentUnitAnalysisUseCase,
    FeedItemAnalysisUseCase,
)


class _Session:
    def __enter__(self) -> _Session:
        return self

    def __exit__(self, *_arguments: Any) -> bool:
        return False

    def begin(self) -> _Session:
        return self

    def get(self, *_arguments: Any) -> object:
        return object()


class _Repository:
    def __init__(self, unit_id: int, *, members: list[str] | None = None) -> None:
        self.calls: list[tuple[str, str, int]] = []
        self.summary = SimpleNamespace(
            summary="Одна проверенная выжимка",
            fingerprint=f"v1:{'a' * 64}",
        )
        self.snapshot = SimpleNamespace(
            unit=SimpleNamespace(id=unit_id, members=members or ["member-a", "member-b"]),
            input_fingerprint="input-fingerprint",
            summary_result=None,
            importance_result=None,
        )

    def _record(self, method: str, kind: str, unit_id: int) -> None:
        self.calls.append((method, kind, unit_id))

    def load_snapshot(self, _session: _Session, *, kind: str, unit_id: int) -> Any:
        self._record("load_snapshot", kind, unit_id)
        return self.snapshot

    def save_summary(
        self,
        _session: _Session,
        *,
        kind: str,
        unit_id: int,
        expected_input_fingerprint: str,
        result: Any,
    ) -> SaveDisposition:
        self._record("save_summary", kind, unit_id)
        assert expected_input_fingerprint == self.snapshot.input_fingerprint
        assert result is self.summary
        return SaveDisposition.CURRENT

    def save_importance(
        self,
        _session: _Session,
        *,
        kind: str,
        unit_id: int,
        expected_input_fingerprint: str,
        expected_summary_lineage_fingerprint: str,
        result: Any,
    ) -> SaveDisposition:
        self._record("save_importance", kind, unit_id)
        assert expected_input_fingerprint == self.snapshot.input_fingerprint
        assert expected_summary_lineage_fingerprint.startswith("summary-lineage-v1:")
        return SaveDisposition.CURRENT

    def get_relevance_prior(
        self,
        _session: _Session,
        *,
        user_id: int,
        kind: str,
        unit_id: int,
        expected_summary_lineage_fingerprint: str,
        interests_fingerprint: str,
    ) -> None:
        self._record("get_relevance_prior", kind, unit_id)
        assert user_id > 0
        assert expected_summary_lineage_fingerprint.startswith("summary-lineage-v1:")
        assert interests_fingerprint
        return None

    def save_relevance(
        self,
        _session: _Session,
        *,
        user_id: int,
        kind: str,
        unit_id: int,
        source_input_fingerprint: str,
        expected_summary_lineage_fingerprint: str,
        interests_fingerprint: str,
        result: Any,
    ) -> SaveDisposition:
        self._record("save_relevance", kind, unit_id)
        assert user_id > 0
        assert source_input_fingerprint == self.snapshot.input_fingerprint
        assert expected_summary_lineage_fingerprint.startswith("summary-lineage-v1:")
        assert interests_fingerprint
        assert result is not None
        return SaveDisposition.CURRENT


class _SummarizationService:
    def __init__(self, result: Any) -> None:
        self.result = result
        self.units: list[Any] = []

    def validate_configuration(self) -> None:
        return None

    def requires_provider_call(self, _unit: Any, *, prior_result: Any, force: bool) -> bool:
        assert prior_result is None
        assert force is False
        return True

    async def summarize(self, unit: Any, *, prior_result: Any, force: bool) -> Any:
        assert prior_result is None
        assert force is False
        self.units.append(unit)
        return SimpleNamespace(result=self.result, reused=False)


class _ScoringService:
    def __init__(self) -> None:
        self.inputs: list[dict[str, Any]] = []

    def validate_configuration(self) -> None:
        return None

    async def score_importance(
        self, value: dict[str, Any], *, prior_result: Any, force: bool
    ) -> Any:
        assert prior_result is None
        assert force is False
        self.inputs.append(value)
        return SimpleNamespace(result=SimpleNamespace(total_score=7.0), reused=False)

    async def score_relevance(
        self, value: dict[str, Any], *, prior_result: Any, force: bool
    ) -> Any:
        assert prior_result is None
        assert force is False
        self.inputs.append(value)
        return SimpleNamespace(result=SimpleNamespace(relevance_score=0.8), reused=False)


def _session_factory() -> _Session:
    return _Session()


def test_cluster_analysis_uses_one_content_unit_snapshot_and_same_summary_for_both_scores() -> None:
    unit_id = 82
    repository = _Repository(unit_id, members=["member-a", "member-b", "member-c"])
    summarization = _SummarizationService(repository.summary)
    importance = _ScoringService()
    relevance = _ScoringService()
    use_case = ContentUnitAnalysisUseCase(
        session_factory=_session_factory,
        repository=repository,
        summarization_service=summarization,
        importance_service=importance,
        relevance_service=relevance,
        kind="cluster",
    )

    outcome = asyncio.run(
        use_case.run(
            unit_id,
            operation=AnalysisOperation.ALL,
            user_id=14,
            interests=["искусственный интеллект"],
        )
    )

    assert outcome.kind == "cluster"
    assert outcome.unit_id == unit_id
    assert outcome.feed_item_id is None
    assert outcome.succeeded
    assert repository.calls == [
        ("load_snapshot", "cluster", unit_id),
        ("save_summary", "cluster", unit_id),
        ("save_importance", "cluster", unit_id),
        ("get_relevance_prior", "cluster", unit_id),
        ("save_relevance", "cluster", unit_id),
    ]
    assert summarization.units == [repository.snapshot.unit]
    assert len(repository.snapshot.unit.members) == 3
    assert len(importance.inputs) == len(relevance.inputs) == 1
    assert importance.inputs[0]["summary"] == repository.summary.summary
    assert relevance.inputs[0]["summary"] == repository.summary.summary
    assert importance.inputs[0]["summary"] == relevance.inputs[0]["summary"]


def test_legacy_feed_item_use_case_keeps_feed_kind_and_outcome_property() -> None:
    unit_id = 29
    repository = _Repository(unit_id)
    summarization = _SummarizationService(repository.summary)
    use_case = FeedItemAnalysisUseCase(
        session_factory=_session_factory,
        repository=repository,
        summarization_service=summarization,
    )

    outcome = asyncio.run(use_case.run(feed_item_id=unit_id, operation=AnalysisOperation.SUMMARY))

    assert outcome.kind == "feed_item"
    assert outcome.unit_id == unit_id
    assert outcome.feed_item_id == unit_id
    assert repository.calls == [
        ("load_snapshot", "feed_item", unit_id),
        ("save_summary", "feed_item", unit_id),
    ]
    assert len(summarization.units) == 1


def test_content_unit_analysis_kind_is_validated() -> None:
    with pytest.raises(ValueError, match="kind"):
        ContentUnitAnalysisUseCase(session_factory=_session_factory, kind="other")  # type: ignore[arg-type]


def test_cluster_membership_error_is_request_error_before_any_provider_call() -> None:
    class _RejectingRepository(_Repository):
        def load_snapshot(self, _session: _Session, *, kind: str, unit_id: int) -> Any:
            self._record("load_snapshot", kind, unit_id)
            raise FeedItemInClusterError(94)

    repository = _RejectingRepository(28)
    summarization = _SummarizationService(repository.summary)
    importance = _ScoringService()
    relevance = _ScoringService()
    use_case = ContentUnitAnalysisUseCase(
        session_factory=_session_factory,
        repository=repository,
        summarization_service=summarization,
        importance_service=importance,
        relevance_service=relevance,
        kind="feed_item",
    )

    with pytest.raises(AnalysisRequestError, match="кластер 94"):
        asyncio.run(use_case.run(28, operation=AnalysisOperation.ALL, user_id=14, interests=["ИИ"]))

    assert repository.calls == [("load_snapshot", "feed_item", 28)]
    assert summarization.units == []
    assert importance.inputs == []
    assert relevance.inputs == []
