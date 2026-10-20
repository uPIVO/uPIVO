from __future__ import annotations

import asyncio
import hashlib
from contextlib import nullcontext
from dataclasses import dataclass, field
from typing import Any

import pytest

from app.ai.analysis_metadata import (
    FeedItemAIMetadata,
    UserItemScoreAIMetadata,
    summary_lineage_fingerprint,
)
from app.ai.base import LLMError
from app.ai.dto import (
    ImportanceComponents,
    ImportanceResult,
    ImportanceRun,
    ImportanceWeights,
    RelevanceResult,
    RelevanceRun,
    SummarizationRun,
    SummaryResult,
)
from app.ai.repository import FeedItemAnalysisRepository
from app.ai.service import SummarizationService
from app.models.content_cluster import ClusterItem
from app.models.feed_item import FeedItem
from app.models.user import User
from app.models.user_item_score import UserItemScore
from app.services.analyze_feed_item import (
    AnalysisOperation,
    AnalysisRequestError,
    AnalysisStatus,
    FeedItemAnalysisUseCase,
)


class _OneRow:
    def __init__(self, value: Any) -> None:
        self.value = value

    def one_or_none(self) -> Any:
        return self.value


class _Rows:
    def __init__(self, values: list[Any]) -> None:
        self.values = values

    def all(self) -> list[Any]:
        return self.values


@dataclass
class _State:
    users: set[int] = field(default_factory=lambda: {17})
    active_sessions: int = 0
    source_name: str = "Источник"
    score: UserItemScore | None = None
    scores: list[UserItemScore] = field(default_factory=list)
    row: FeedItem = field(
        default_factory=lambda: FeedItem(
            id=7,
            source_id=3,
            external_id="post-7",
            title="Заголовок",
            body_text="Исходный текст A",
            original_url="https://example.test/item",
            extracted_entities={"tags": ["AI"]},
            raw_payload={"private": "не передавать в анализ"},
            summary=None,
            importance_score=None,
            ai_metadata=None,
        )
    )

    def session(self) -> _Session:
        return _Session(self)


class _Session:
    def __init__(self, state: _State) -> None:
        self.state = state

    def __enter__(self) -> _Session:
        self.state.active_sessions += 1
        return self

    def __exit__(self, *_: object) -> None:
        self.state.active_sessions -= 1

    def begin(self):
        return nullcontext()

    def get(self, model: type, identifier: int) -> object | None:
        if model is User and identifier in self.state.users:
            return object()
        return None

    def execute(self, _statement: Any) -> _OneRow:
        return _OneRow((self.state.row, self.state.source_name))

    def scalar(self, _statement: Any) -> UserItemScore | None:
        if _statement.column_descriptions[0].get("entity") is ClusterItem:
            return None
        return self.state.score

    def scalars(self, _statement: Any) -> _Rows:
        return _Rows(self.state.scores)

    def add(self, row: UserItemScore) -> None:
        self.state.score = row
        self.state.scores.append(row)

    def flush(self) -> None:
        return None


def _fingerprint(value: str) -> str:
    return f"v1:{hashlib.sha256(value.encode('utf-8')).hexdigest()}"


def _summary_for(text: str) -> SummaryResult:
    return SummaryResult(
        summary=f"Сводка: {text}",
        fingerprint=_fingerprint(text),
        provider="openrouter",
        preset="@preset/test",
    )


def _importance_for(summary: str) -> ImportanceResult:
    components = ImportanceComponents(
        scale_score=1.0,
        significance_score=0.0,
        reaction_score=0.0,
        fingerprint=_fingerprint(summary),
        provider="typesafe",
        model="jev-test",
    )
    weights = ImportanceWeights(scale=1.0, significance=1.0, reaction=1.0)
    return ImportanceResult(
        **components.model_dump(),
        total_score=1.0,
        weights=weights,
        aggregation_version="content-importance-sum-v2",
    )


def _relevance_for(summary: str, interests: list[str]) -> RelevanceResult:
    return RelevanceResult(
        relevance=0.8,
        fingerprint=_fingerprint(summary + "\0" + "\0".join(interests)),
        provider="typesafe",
        model="jev-test",
    )


class _FingerprintOnlyAdapter:
    def validate_configuration(self) -> None:
        raise AssertionError("Ключ не должен проверяться при определении cache hit")

    def fingerprint_for(self, unit: Any) -> str:
        return _fingerprint(unit.feed_items[0].body_text)

    async def summarize(self, _unit: Any) -> SummaryResult:
        raise AssertionError("Провайдер не должен вызываться при cache hit")


class _SummaryService:
    def __init__(
        self,
        state: _State,
        *,
        mutate_calls: set[int] | None = None,
        fail: bool = False,
        fail_config: bool = False,
    ) -> None:
        self.state = state
        self.mutate_calls = mutate_calls or set()
        self.fail = fail
        self.fail_config = fail_config
        self.calls: list[tuple[SummaryResult | None, bool]] = []
        self.configuration_checks = 0

    def validate_configuration(self) -> None:
        self.configuration_checks += 1
        if self.fail_config:
            raise LLMError("Некорректная конфигурация суммаризации.")

    def requires_provider_call(
        self,
        unit: Any,
        *,
        prior_result: SummaryResult | None = None,
        force: bool = False,
    ) -> bool:
        return (
            force
            or prior_result is None
            or prior_result.fingerprint != _fingerprint(unit.feed_items[0].body_text)
        )

    async def summarize(
        self,
        unit: Any,
        *,
        prior_result: SummaryResult | None = None,
        force: bool = False,
    ) -> SummarizationRun:
        assert self.state.active_sessions == 0
        self.calls.append((prior_result, force))
        if self.fail:
            raise LLMError("Не удалось получить выжимку.")
        if prior_result is not None and not force:
            result = prior_result
            reused = True
        else:
            result = _summary_for(unit.feed_items[0].body_text)
            reused = False
        await asyncio.sleep(0)
        if len(self.calls) in self.mutate_calls:
            self.state.row.body_text += " изменено"
        return SummarizationRun(result=result, reused=reused, elapsed_seconds=0.0)


class _ImportanceService:
    def __init__(
        self,
        state: _State,
        *,
        fail: bool = False,
        fail_config: bool = False,
        mutate_calls: set[int] | None = None,
    ) -> None:
        self.state = state
        self.fail = fail
        self.fail_config = fail_config
        self.mutate_calls = mutate_calls or set()
        self.calls: list[tuple[str, ImportanceResult | None, bool]] = []
        self.configuration_checks = 0

    def validate_configuration(self) -> None:
        self.configuration_checks += 1
        if self.fail_config:
            raise LLMError("Некорректная конфигурация TypeSafe.")

    async def score_importance(
        self,
        value: dict[str, str],
        *,
        prior_result: ImportanceResult | None = None,
        force: bool = False,
    ) -> ImportanceRun:
        assert self.state.active_sessions == 0
        summary = value["summary"]
        self.calls.append((summary, prior_result, force))
        await asyncio.sleep(0)
        if self.fail:
            raise LLMError("Не удалось получить оценку общей важности.")
        if len(self.calls) in self.mutate_calls:
            self.state.row.body_text += " изменено"
        if prior_result is not None and not force:
            return ImportanceRun(result=prior_result, reused=True, elapsed_seconds=0.0)
        return ImportanceRun(result=_importance_for(summary), reused=False, elapsed_seconds=0.0)


class _RelevanceService:
    def __init__(self, state: _State, *, fail: bool = False, fail_config: bool = False) -> None:
        self.state = state
        self.fail = fail
        self.fail_config = fail_config
        self.calls: list[tuple[str, list[str], RelevanceResult | None, bool]] = []
        self.configuration_checks = 0

    def validate_configuration(self) -> None:
        self.configuration_checks += 1
        if self.fail_config:
            raise LLMError("Некорректная конфигурация TypeSafe.")

    async def score_relevance(
        self,
        value: dict[str, Any],
        *,
        prior_result: RelevanceResult | None = None,
        force: bool = False,
    ) -> RelevanceRun:
        assert self.state.active_sessions == 0
        summary = value["summary"]
        interests = list(value["interests"])
        self.calls.append((summary, interests, prior_result, force))
        await asyncio.sleep(0)
        if self.fail:
            raise LLMError("Не удалось получить оценку релевантности.")
        if prior_result is not None and not force:
            return RelevanceRun(result=prior_result, reused=True, elapsed_seconds=0.0)
        return RelevanceRun(
            result=_relevance_for(summary, interests), reused=False, elapsed_seconds=0.0
        )


def _use_case(
    state: _State,
    *,
    summary: _SummaryService | None = None,
    importance: _ImportanceService | None = None,
    relevance: _RelevanceService | None = None,
) -> FeedItemAnalysisUseCase:
    return FeedItemAnalysisUseCase(
        session_factory=state.session,
        repository=FeedItemAnalysisRepository(),
        summarization_service=summary,
        importance_service=importance,
        relevance_service=relevance,
    )


def _run(awaitable: Any) -> Any:
    return asyncio.run(awaitable)


def test_summary_cache_preflight_preserves_reuse_without_provider_credentials() -> None:
    state = _State()
    with state.session() as session, session.begin():
        snapshot = FeedItemAnalysisRepository().load_snapshot(session, kind="feed_item", unit_id=7)
    assert snapshot is not None
    service = SummarizationService(_FingerprintOnlyAdapter())
    prior = _summary_for(state.row.body_text)

    assert not service.requires_provider_call(snapshot.unit, prior_result=prior)
    assert service.requires_provider_call(snapshot.unit, prior_result=prior, force=True)
    run = asyncio.run(service.summarize(snapshot.unit, prior_result=prior))
    assert run.reused and run.result == prior


def test_all_runs_independent_scores_and_never_holds_session_across_await() -> None:
    state = _State()
    summary = _SummaryService(state)
    importance = _ImportanceService(state, fail=True)
    relevance = _RelevanceService(state)

    outcome = _run(
        _use_case(state, summary=summary, importance=importance, relevance=relevance).run(
            7,
            operation=AnalysisOperation.ALL,
            user_id=17,
            interests=["частный интерес"],
        )
    )

    assert outcome.attempt_count == 1
    assert outcome.summary.status == AnalysisStatus.CURRENT
    assert outcome.importance.status == AnalysisStatus.FAILED
    assert outcome.relevance.status == AnalysisStatus.CURRENT
    assert len(importance.calls) == 1
    assert len(relevance.calls) == 1
    assert "частный интерес" not in str(state.score.analysis_metadata)
    assert state.active_sessions == 0


def test_stale_all_retries_complete_requested_set_once_from_fresh_snapshot() -> None:
    state = _State()
    summary = _SummaryService(state, mutate_calls={1})
    importance = _ImportanceService(state)
    relevance = _RelevanceService(state)

    outcome = _run(
        _use_case(state, summary=summary, importance=importance, relevance=relevance).run(
            7,
            operation=AnalysisOperation.ALL,
            user_id=17,
            interests=["тема"],
        )
    )

    assert outcome.attempt_count == 2
    assert [call[0].summary if call[0] is not None else None for call in summary.calls] == [
        None,
        None,
    ]
    assert len(importance.calls) == 1
    assert len(relevance.calls) == 1
    assert outcome.summary.status == AnalysisStatus.CURRENT
    assert outcome.importance.status == AnalysisStatus.CURRENT
    assert outcome.relevance.status == AnalysisStatus.CURRENT
    stored = FeedItemAIMetadata.model_validate(state.row.ai_metadata)
    score_metadata = UserItemScoreAIMetadata.model_validate(state.score.analysis_metadata)
    assert stored.summary.stale_candidate.result.summary == "Сводка: Исходный текст A"
    assert stored.importance.stale_candidate is None
    assert score_metadata.relevance.stale_candidate is None
    assert state.row.summary == "Сводка: Исходный текст A изменено"
    assert state.active_sessions == 0


def test_all_skips_scores_when_summary_save_is_stale_twice() -> None:
    state = _State()
    summary = _SummaryService(state, mutate_calls={1, 2})
    importance = _ImportanceService(state)
    relevance = _RelevanceService(state)

    outcome = _run(
        _use_case(state, summary=summary, importance=importance, relevance=relevance).run(
            7,
            operation=AnalysisOperation.ALL,
            user_id=17,
            interests=["тема"],
        )
    )

    assert outcome.attempt_count == 2
    assert outcome.summary.status == AnalysisStatus.STALE_CANDIDATE
    assert outcome.importance.status == AnalysisStatus.SKIPPED
    assert outcome.relevance.status == AnalysisStatus.SKIPPED
    assert not importance.calls and not relevance.calls
    assert state.score is None


def test_score_time_mutation_preserves_paid_stale_result_then_retries() -> None:
    state = _State()
    summary = _SummaryService(state)
    importance = _ImportanceService(state, mutate_calls={1})
    relevance = _RelevanceService(state)

    outcome = _run(
        _use_case(state, summary=summary, importance=importance, relevance=relevance).run(
            7,
            operation=AnalysisOperation.ALL,
            user_id=17,
            interests=["тема"],
        )
    )

    assert outcome.attempt_count == 2
    assert len(importance.calls) == 2
    assert len(relevance.calls) == 1
    assert outcome.summary.status == AnalysisStatus.CURRENT
    assert outcome.importance.status == AnalysisStatus.CURRENT
    assert outcome.relevance.status == AnalysisStatus.CURRENT
    stored = FeedItemAIMetadata.model_validate(state.row.ai_metadata)
    assert stored.importance.stale_candidate.result.total_score == 1.0
    assert state.row.summary == "Сводка: Исходный текст A изменено"


def test_repeated_all_reuses_current_summary_and_both_scores() -> None:
    state = _State()
    summary = _SummaryService(state)
    importance = _ImportanceService(state)
    relevance = _RelevanceService(state)
    use_case = _use_case(state, summary=summary, importance=importance, relevance=relevance)

    first = _run(
        use_case.run(
            7,
            operation=AnalysisOperation.ALL,
            user_id=17,
            interests=["тема"],
        )
    )
    summary.fail_config = True
    importance.fail_config = True
    relevance.fail_config = True
    second = _run(
        use_case.run(
            7,
            operation=AnalysisOperation.ALL,
            user_id=17,
            interests=["тема"],
        )
    )

    assert first.succeeded and second.succeeded
    assert second.attempt_count == 1
    assert second.summary.reused is True
    assert second.importance.reused is True
    assert second.relevance.reused is True
    assert summary.calls[1][0] == first.summary.result
    assert importance.calls[1][1] == first.importance.result
    assert relevance.calls[1][2] == first.relevance.result
    assert summary.configuration_checks == 1
    assert importance.configuration_checks == 1
    assert relevance.configuration_checks == 1


def test_continuing_source_mutation_stops_after_two_passes_without_publishing_stale() -> None:
    state = _State()
    summary = _SummaryService(state, mutate_calls={1, 2})

    outcome = _run(_use_case(state, summary=summary).run(7, operation=AnalysisOperation.SUMMARY))

    assert outcome.attempt_count == 2
    assert len(summary.calls) == 2
    assert outcome.summary.status == AnalysisStatus.STALE_CANDIDATE
    assert state.row.summary is None
    stored = FeedItemAIMetadata.model_validate(state.row.ai_metadata)
    assert stored.summary.last_good is None
    assert stored.summary.stale_candidate.result.summary == "Сводка: Исходный текст A изменено"


def test_provider_failure_preserves_last_good_and_does_not_block_other_score() -> None:
    state = _State()
    repository = FeedItemAnalysisRepository()
    with state.session() as session, session.begin():
        snapshot = repository.load_snapshot(session, kind="feed_item", unit_id=7)
        assert snapshot is not None
        summary_result = _summary_for("Исходный текст A")
        repository.save_summary(
            session,
            feed_item_id=7,
            expected_input_fingerprint=snapshot.input_fingerprint,
            result=summary_result,
        )
        repository.save_importance(
            session,
            feed_item_id=7,
            expected_input_fingerprint=snapshot.input_fingerprint,
            expected_summary_lineage_fingerprint=summary_lineage_fingerprint(summary_result),
            result=_importance_for(summary_result.summary),
        )
    old_score = state.row.importance_score
    importance = _ImportanceService(state, fail=True)
    outcome = _run(
        _use_case(state, importance=importance).run(7, operation=AnalysisOperation.IMPORTANCE)
    )

    assert outcome.importance.status == AnalysisStatus.FAILED
    assert state.row.importance_score == old_score
    assert state.active_sessions == 0


def test_relevance_requires_existing_user_and_valid_context_before_paid_work() -> None:
    state = _State(users=set())
    summary = _SummaryService(state)
    importance = _ImportanceService(state)
    relevance = _RelevanceService(state)

    with pytest.raises(AnalysisRequestError):
        _run(
            _use_case(state, summary=summary, importance=importance, relevance=relevance).run(
                7,
                operation=AnalysisOperation.RELEVANCE,
                user_id=17,
                interests=["секретная тема"],
            )
        )

    assert not summary.calls and not importance.calls and not relevance.calls
    assert state.active_sessions == 0


def test_score_only_operation_requires_current_summary_without_summarizing() -> None:
    state = _State()
    importance = _ImportanceService(state)

    with pytest.raises(AnalysisRequestError):
        _run(_use_case(state, importance=importance).run(7, operation=AnalysisOperation.IMPORTANCE))

    assert not importance.calls
    assert state.active_sessions == 0


def test_invalid_interests_and_bad_provider_config_fail_before_paid_work() -> None:
    state = _State()
    summary = _SummaryService(state)
    importance = _ImportanceService(state, fail_config=True)
    relevance = _RelevanceService(state)
    use_case = _use_case(state, summary=summary, importance=importance, relevance=relevance)

    with pytest.raises(AnalysisRequestError):
        _run(
            use_case.run(
                7,
                operation=AnalysisOperation.RELEVANCE,
                user_id=17,
                interests=["   "],
            )
        )
    with pytest.raises(LLMError):
        _run(
            use_case.run(
                7,
                operation=AnalysisOperation.ALL,
                user_id=17,
                interests=["тема"],
            )
        )

    assert not summary.calls and not importance.calls and not relevance.calls


def test_summary_failure_skips_both_score_operations() -> None:
    state = _State()
    summary = _SummaryService(state, fail=True)
    importance = _ImportanceService(state)
    relevance = _RelevanceService(state)

    outcome = _run(
        _use_case(state, summary=summary, importance=importance, relevance=relevance).run(
            7,
            operation=AnalysisOperation.ALL,
            user_id=17,
            interests=["тема"],
        )
    )

    assert outcome.summary.status == AnalysisStatus.FAILED
    assert outcome.importance.status == AnalysisStatus.SKIPPED
    assert outcome.relevance.status == AnalysisStatus.SKIPPED
    assert not importance.calls and not relevance.calls


def test_force_and_prior_are_forwarded_to_existing_summary_service() -> None:
    state = _State()
    repository = FeedItemAnalysisRepository()
    with state.session() as session, session.begin():
        snapshot = repository.load_snapshot(session, kind="feed_item", unit_id=7)
        assert snapshot is not None
        prior = _summary_for("Исходный текст A")
        repository.save_summary(
            session,
            feed_item_id=7,
            expected_input_fingerprint=snapshot.input_fingerprint,
            result=prior,
        )
    summary = _SummaryService(state)

    first = _run(_use_case(state, summary=summary).run(7, operation=AnalysisOperation.SUMMARY))
    second = _run(
        _use_case(state, summary=summary).run(7, operation=AnalysisOperation.SUMMARY, force=True)
    )

    assert summary.calls[0] == (prior, False)
    assert summary.calls[1] == (prior, True)
    assert first.summary.reused is True
    assert second.summary.reused is False
