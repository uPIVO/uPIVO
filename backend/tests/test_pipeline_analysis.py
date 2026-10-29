"""Общий AI-этап конвейера: отбор единиц, операции, статусы и повторы.

Провайдеры и брокер заменены фейками: проверяется маршрутизация, сохранение,
переиспользование и повторная постановка, а не качество AI и доступность API.
"""

from __future__ import annotations

import asyncio
import hashlib
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.ai.base import LLMError
from app.ai.dto import ImportanceResult, SummaryResult
from app.ai.importance import IMPORTANCE_AGGREGATION_VERSION, IMPORTANCE_WEIGHTS
from app.ai.repository import ContentUnitAnalysisRepository
from app.models.content_cluster import ClusterItem, ContentCluster
from app.models.enums import ProcessingStatus, SourceType
from app.models.feed_item import FeedItem
from app.models.source import Source
from app.pipeline.analysis import AnalysisStats, run_common_analysis

_TABLES = (
    "CREATE TABLE sources (id INTEGER PRIMARY KEY AUTOINCREMENT, type TEXT NOT NULL, "
    "name TEXT NOT NULL, canonical_url TEXT NOT NULL, external_id TEXT, "
    "last_synced_at DATETIME, created_at DATETIME)",
    "CREATE TABLE feed_items (id INTEGER PRIMARY KEY AUTOINCREMENT, "
    "source_id INTEGER NOT NULL, external_id TEXT NOT NULL, title TEXT, "
    "body_text TEXT NOT NULL, original_url TEXT NOT NULL, author TEXT, "
    "published_at DATETIME, collected_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, "
    "raw_payload TEXT, extracted_entities TEXT, content_hash TEXT, summary TEXT, "
    "importance_score REAL, ai_metadata TEXT, "
    "processing_status TEXT NOT NULL DEFAULT 'pending', "
    "created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at DATETIME)",
    "CREATE TABLE content_clusters (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, "
    "summary TEXT, importance_score REAL, ai_metadata TEXT, created_at DATETIME)",
    "CREATE TABLE cluster_items (id INTEGER PRIMARY KEY AUTOINCREMENT, "
    "cluster_id INTEGER NOT NULL, feed_item_id INTEGER NOT NULL, "
    "is_primary BOOLEAN NOT NULL)",
    "CREATE TABLE user_item_scores (id INTEGER PRIMARY KEY AUTOINCREMENT, "
    "user_id INTEGER NOT NULL, feed_item_id INTEGER, cluster_id INTEGER, "
    "relevance_score REAL, rank_score REAL, reason TEXT, "
    "calculated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, analysis_metadata TEXT)",
)


def _digest(seed: str) -> str:
    return f"v1:{hashlib.sha256(seed.encode()).hexdigest()}"


def _unit_fingerprint(unit: Any) -> str:
    body = "|".join(sorted(item.body_text for item in unit.feed_items))
    return _digest(f"{unit.kind}:{unit.id}:{body}")


def _summary_result(summary: str, fingerprint: str) -> SummaryResult:
    return SummaryResult(
        summary=summary,
        fingerprint=fingerprint,
        provider="test",
        preset="test",
    )


def _importance_result(summary: str) -> ImportanceResult:
    return ImportanceResult(
        scale_score=2.0,
        significance_score=1.0,
        reaction_score=0.5,
        total_score=3.5,
        weights=IMPORTANCE_WEIGHTS,
        aggregation_version=IMPORTANCE_AGGREGATION_VERSION,
        fingerprint=_digest(f"importance:{summary}"),
        provider="test",
        model="test",
    )


class _Summarizer:
    """Фейковый SummarizationService без провайдера."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.failing: set[int] = set()

    def validate_configuration(self) -> None:
        return None

    def requires_provider_call(
        self, unit: Any, *, prior_result: Any = None, force: bool = False
    ) -> bool:
        assert force is False
        return prior_result is None or prior_result.fingerprint != _unit_fingerprint(unit)

    async def summarize(
        self, unit: Any, *, prior_result: Any = None, force: bool = False
    ) -> Any:
        fingerprint = _unit_fingerprint(unit)
        if prior_result is not None and not force and prior_result.fingerprint == fingerprint:
            return SimpleNamespace(result=prior_result, reused=True)
        self.calls.append(f"{unit.kind}:{unit.id}")
        if unit.id in self.failing:
            raise LLMError("Провайдер выжимки недоступен")
        return SimpleNamespace(
            result=_summary_result(f"Выжимка {unit.kind} {unit.id}", fingerprint),
            reused=False,
        )


class _Importance:
    """Фейковый ImportanceService без провайдера."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.failing: set[str] = set()

    def validate_configuration(self) -> None:
        return None

    async def score_importance(
        self, value: Any, *, prior_result: Any = None, force: bool = False
    ) -> Any:
        summary = value["summary"]
        fingerprint = _digest(f"importance:{summary}")
        if (
            prior_result is not None
            and not force
            and prior_result.fingerprint == fingerprint
            and prior_result.aggregation_version == IMPORTANCE_AGGREGATION_VERSION
            and prior_result.weights == IMPORTANCE_WEIGHTS
        ):
            return SimpleNamespace(result=prior_result, reused=True)
        self.calls.append(summary)
        if summary in self.failing:
            raise LLMError("Провайдер важности недоступен")
        return SimpleNamespace(result=_importance_result(summary), reused=False)


class _Dispatcher:
    """Фейковый диспетчер персональной оценки вместо брокера."""

    def __init__(self, *, fail: bool = False) -> None:
        self.posted: list[tuple[str, int]] = []
        self.fail = fail
        self.on_post: Any = None

    def __call__(self, kind: str, unit_id: int) -> None:
        if self.fail:
            raise RuntimeError("брокер недоступен")
        self.posted.append((kind, unit_id))
        if self.on_post is not None:
            self.on_post(kind, unit_id)


@pytest.fixture
def factory():
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    with engine.begin() as connection:
        for statement in _TABLES:
            connection.execute(text(statement))
    yield sessionmaker(bind=engine)
    engine.dispose()


def _add_items(
    factory: sessionmaker[Session], count: int = 1, *, status: ProcessingStatus | None = None
) -> tuple[int, list[int]]:
    with factory() as session:
        source = Source(
            type=SourceType.TELEGRAM,
            name="Источник",
            external_id="ai-source",
            canonical_url="https://example.test/ai-source",
        )
        session.add(source)
        session.flush()
        items = [
            FeedItem(
                source_id=source.id,
                external_id=f"ai-item-{index}",
                title=f"Новость {index}",
                body_text=f"Факты {index}",
                original_url=f"https://example.test/ai-item-{index}",
                processing_status=status or ProcessingStatus.NORMALIZED,
            )
            for index in range(count)
        ]
        session.add_all(items)
        session.flush()
        source_id = source.id
        item_ids = [item.id for item in items]
        session.commit()
        return source_id, item_ids


def _add_cluster(factory: sessionmaker[Session], item_ids: list[int]) -> int:
    with factory() as session:
        cluster = ContentCluster(title="Общее событие")
        session.add(cluster)
        session.flush()
        session.add_all(
            [
                ClusterItem(
                    cluster_id=cluster.id,
                    feed_item_id=item_id,
                    is_primary=index == 0,
                )
                for index, item_id in enumerate(item_ids)
            ]
        )
        cluster_id = cluster.id
        session.commit()
        return cluster_id


def _statuses(factory: sessionmaker[Session]) -> dict[int, ProcessingStatus]:
    with factory() as session:
        return dict(session.execute(select(FeedItem.id, FeedItem.processing_status)).all())


def _feed_item(factory: sessionmaker[Session], item_id: int) -> FeedItem:
    with factory() as session:
        return session.get(FeedItem, item_id)


def _cluster_summary(factory: sessionmaker[Session], cluster_id: int) -> str | None:
    with factory() as session:
        return session.get(ContentCluster, cluster_id).summary


def _run(
    factory: sessionmaker[Session],
    *,
    summarizer: _Summarizer,
    importance: _Importance,
    dispatch: _Dispatcher,
    ai_limit: int | None = 50,
) -> AnalysisStats:
    return asyncio.run(
        run_common_analysis(
            session_factory=factory,
            ai_limit=ai_limit,
            summarization_service=summarizer,
            importance_service=importance,
            dispatch=dispatch,
        )
    )


def test_cluster_is_analyzed_once_and_marks_all_members(factory):
    _source_id, item_ids = _add_items(factory, 2)
    cluster_id = _add_cluster(factory, item_ids)
    summarizer, importance, dispatcher = _Summarizer(), _Importance(), _Dispatcher()

    stats = _run(factory, summarizer=summarizer, importance=importance, dispatch=dispatcher)

    assert summarizer.calls == [f"cluster:{cluster_id}"]
    assert importance.calls == [f"Выжимка cluster {cluster_id}"]
    assert dispatcher.posted == [("cluster", cluster_id)]
    assert _statuses(factory) == {
        item_ids[0]: ProcessingStatus.PROCESSED,
        item_ids[1]: ProcessingStatus.PROCESSED,
    }
    assert _cluster_summary(factory, cluster_id) == f"Выжимка cluster {cluster_id}"
    assert all(_feed_item(factory, item_id).summary is None for item_id in item_ids)
    assert stats == AnalysisStats(
        selected=1,
        succeeded=1,
        queue_succeeded=1,
        summary_failed=0,
        importance_failed=0,
    )


def test_singleton_becomes_processed_only_after_both_operations(factory):
    _source_id, item_ids = _add_items(factory, 1)
    summarizer, importance, dispatcher = _Summarizer(), _Importance(), _Dispatcher()

    stats = _run(factory, summarizer=summarizer, importance=importance, dispatch=dispatcher)

    assert summarizer.calls == [f"feed_item:{item_ids[0]}"]
    assert importance.calls == [f"Выжимка feed_item {item_ids[0]}"]
    assert dispatcher.posted == [("feed_item", item_ids[0])]
    assert _statuses(factory) == {item_ids[0]: ProcessingStatus.PROCESSED}
    assert _feed_item(factory, item_ids[0]).summary == f"Выжимка feed_item {item_ids[0]}"
    assert stats.succeeded == 1


def test_rerun_without_changes_makes_no_provider_calls(factory):
    _source_id, item_ids = _add_items(factory, 1)
    summarizer, importance, dispatcher = _Summarizer(), _Importance(), _Dispatcher()
    _run(factory, summarizer=summarizer, importance=importance, dispatch=dispatcher)

    stats = _run(factory, summarizer=summarizer, importance=importance, dispatch=dispatcher)

    assert summarizer.calls == [f"feed_item:{item_ids[0]}"]
    assert len(importance.calls) == 1
    assert dispatcher.posted == [("feed_item", item_ids[0])]
    assert stats == AnalysisStats()


def test_changed_body_text_forces_new_summary_and_importance(factory):
    _source_id, item_ids = _add_items(factory, 1)
    summarizer, importance, dispatcher = _Summarizer(), _Importance(), _Dispatcher()
    _run(factory, summarizer=summarizer, importance=importance, dispatch=dispatcher)
    with factory() as session:
        session.get(FeedItem, item_ids[0]).body_text = "Обновлённые факты"
        session.commit()

    stats = _run(factory, summarizer=summarizer, importance=importance, dispatch=dispatcher)

    assert summarizer.calls == [f"feed_item:{item_ids[0]}"] * 2
    assert importance.calls == [f"Выжимка feed_item {item_ids[0]}"] * 2
    assert dispatcher.posted == [("feed_item", item_ids[0])] * 2
    assert stats.succeeded == 1
    assert _feed_item(factory, item_ids[0]).summary == f"Выжимка feed_item {item_ids[0]}"


def test_importance_failure_keeps_summary_and_retries_without_new_summary(factory):
    _source_id, item_ids = _add_items(factory, 1)
    summarizer, importance, dispatcher = _Summarizer(), _Importance(), _Dispatcher()
    importance.failing.add(f"Выжимка feed_item {item_ids[0]}")

    failed = _run(factory, summarizer=summarizer, importance=importance, dispatch=dispatcher)

    item = _feed_item(factory, item_ids[0])
    assert item.summary == f"Выжимка feed_item {item_ids[0]}"
    assert item.importance_score is None
    assert _statuses(factory) == {item_ids[0]: ProcessingStatus.NORMALIZED}
    assert dispatcher.posted == []
    assert failed.failed == 1
    assert failed.importance_failed == 1
    assert failed.summary_failed == 0
    assert failed.has_failures is True

    importance.failing.clear()
    resumed = _run(factory, summarizer=summarizer, importance=importance, dispatch=dispatcher)

    assert summarizer.calls == [f"feed_item:{item_ids[0]}"]
    assert _statuses(factory) == {item_ids[0]: ProcessingStatus.PROCESSED}
    assert dispatcher.posted == [("feed_item", item_ids[0])]
    assert resumed.succeeded == 1


def test_summary_failure_does_not_block_other_units(factory):
    _source_id, item_ids = _add_items(factory, 2)
    summarizer, importance, dispatcher = _Summarizer(), _Importance(), _Dispatcher()
    summarizer.failing.add(item_ids[0])

    stats = _run(factory, summarizer=summarizer, importance=importance, dispatch=dispatcher)

    assert _statuses(factory) == {
        item_ids[0]: ProcessingStatus.NORMALIZED,
        item_ids[1]: ProcessingStatus.PROCESSED,
    }
    assert dispatcher.posted == [("feed_item", item_ids[1])]
    assert stats.summary_failed == 1
    assert stats.succeeded == 1


def test_broker_failure_keeps_unit_unprocessed_and_rerun_posts_again(factory):
    _source_id, item_ids = _add_items(factory, 1)
    summarizer, importance = _Summarizer(), _Importance()
    broken = _Dispatcher(fail=True)

    failed = _run(factory, summarizer=summarizer, importance=importance, dispatch=broken)

    assert failed.queue_failed == 1
    assert failed.succeeded == 0
    assert _statuses(factory) == {item_ids[0]: ProcessingStatus.NORMALIZED}

    working = _Dispatcher()
    resumed = _run(factory, summarizer=summarizer, importance=importance, dispatch=working)

    assert working.posted == [("feed_item", item_ids[0])]
    assert resumed.succeeded == 1
    assert resumed.queue_succeeded == 1
    assert summarizer.calls == [f"feed_item:{item_ids[0]}"]
    assert len(importance.calls) == 1
    assert _statuses(factory) == {item_ids[0]: ProcessingStatus.PROCESSED}


def test_ai_limit_skips_finished_units_instead_of_blocking_backlog(factory):
    _source_id, item_ids = _add_items(factory, 3)
    summarizer, importance, dispatcher = _Summarizer(), _Importance(), _Dispatcher()

    first = _run(
        factory,
        summarizer=summarizer,
        importance=importance,
        dispatch=dispatcher,
        ai_limit=1,
    )

    assert first.selected == 1
    assert summarizer.calls == [f"feed_item:{item_ids[0]}"]
    assert _statuses(factory) == {
        item_ids[0]: ProcessingStatus.PROCESSED,
        item_ids[1]: ProcessingStatus.NORMALIZED,
        item_ids[2]: ProcessingStatus.NORMALIZED,
    }

    second = _run(
        factory,
        summarizer=summarizer,
        importance=importance,
        dispatch=dispatcher,
        ai_limit=2,
    )

    assert second.selected == 2
    assert summarizer.calls == [f"feed_item:{item_id}" for item_id in item_ids]
    assert set(_statuses(factory).values()) == {ProcessingStatus.PROCESSED}


def test_singleton_clustered_during_run_is_not_marked_processed(factory):
    _source_id, item_ids = _add_items(factory, 3)
    summarizer, importance = _Summarizer(), _Importance()
    dispatcher = _Dispatcher()

    def cluster_during_dispatch(kind: str, unit_id: int) -> None:
        _add_cluster(factory, [unit_id, item_ids[2]])

    dispatcher.on_post = cluster_during_dispatch
    _run(factory, summarizer=summarizer, importance=importance, dispatch=dispatcher)

    statuses = _statuses(factory)
    assert statuses[item_ids[0]] == ProcessingStatus.NORMALIZED
    assert statuses[item_ids[1]] == ProcessingStatus.NORMALIZED


def test_pending_and_failed_units_are_not_sent_to_ai(factory):
    _source_id, failed_ids = _add_items(factory, 1, status=ProcessingStatus.FAILED)
    summarizer, importance, dispatcher = _Summarizer(), _Importance(), _Dispatcher()

    stats = _run(factory, summarizer=summarizer, importance=importance, dispatch=dispatcher)

    assert summarizer.calls == []
    assert dispatcher.posted == []
    assert stats == AnalysisStats()
    assert _statuses(factory) == {failed_ids[0]: ProcessingStatus.FAILED}


def test_non_positive_ai_limit_is_rejected(factory):
    summarizer, importance, dispatcher = _Summarizer(), _Importance(), _Dispatcher()
    with pytest.raises(ValueError, match="ai_limit"):
        _run(factory, summarizer=summarizer, importance=importance, dispatch=dispatcher, ai_limit=0)


def _set_status(factory: sessionmaker[Session], item_id: int, status: ProcessingStatus) -> None:
    with factory() as session:
        session.get(FeedItem, item_id).processing_status = status
        session.commit()


def _snapshot(factory: sessionmaker[Session], kind: str, unit_id: int):
    repository = ContentUnitAnalysisRepository()
    with factory() as session:
        return repository.load_snapshot(session, kind=kind, unit_id=unit_id)


def _confirm(
    factory: sessionmaker[Session],
    *,
    kind: str,
    unit_id: int,
    expected_input_fingerprint: str,
    expected_summary: SummaryResult,
    expected_importance: ImportanceResult,
) -> bool:
    repository = ContentUnitAnalysisRepository()
    with factory() as session:
        with session.begin():
            return repository.mark_unit_processed(
                session,
                kind=kind,
                unit_id=unit_id,
                expected_input_fingerprint=expected_input_fingerprint,
                expected_summary=expected_summary,
                expected_importance=expected_importance,
            )


def _confirm_snapshot(factory: sessionmaker[Session], kind: str, unit_id: int) -> bool:
    snapshot = _snapshot(factory, kind, unit_id)
    return _confirm(
        factory,
        kind=kind,
        unit_id=unit_id,
        expected_input_fingerprint=snapshot.input_fingerprint,
        expected_summary=snapshot.summary_result,
        expected_importance=snapshot.importance_result,
    )


def test_repository_confirms_current_unit_and_is_idempotent(factory):
    _source_id, item_ids = _add_items(factory, 1)
    summarizer, importance, dispatcher = _Summarizer(), _Importance(), _Dispatcher()
    _run(factory, summarizer=summarizer, importance=importance, dispatch=dispatcher)
    _set_status(factory, item_ids[0], ProcessingStatus.NORMALIZED)

    assert _confirm_snapshot(factory, "feed_item", item_ids[0]) is True
    assert _statuses(factory)[item_ids[0]] == ProcessingStatus.PROCESSED
    assert _confirm_snapshot(factory, "feed_item", item_ids[0]) is True


def test_repository_refuses_input_changed_after_analysis(factory):
    _source_id, item_ids = _add_items(factory, 1)
    summarizer, importance, dispatcher = _Summarizer(), _Importance(), _Dispatcher()
    _run(factory, summarizer=summarizer, importance=importance, dispatch=dispatcher)
    snapshot = _snapshot(factory, "feed_item", item_ids[0])
    with factory() as session:
        session.get(FeedItem, item_ids[0]).body_text = "Другие факты"
        session.commit()
    _set_status(factory, item_ids[0], ProcessingStatus.NORMALIZED)

    assert (
        _confirm(
            factory,
            kind="feed_item",
            unit_id=item_ids[0],
            expected_input_fingerprint=snapshot.input_fingerprint,
            expected_summary=snapshot.summary_result,
            expected_importance=snapshot.importance_result,
        )
        is False
    )
    assert _statuses(factory)[item_ids[0]] == ProcessingStatus.NORMALIZED


def test_repository_refuses_cluster_with_unavailable_member(factory):
    _source_id, item_ids = _add_items(factory, 2)
    cluster_id = _add_cluster(factory, item_ids)
    summarizer, importance, dispatcher = _Summarizer(), _Importance(), _Dispatcher()
    _run(factory, summarizer=summarizer, importance=importance, dispatch=dispatcher)
    _set_status(factory, item_ids[0], ProcessingStatus.NORMALIZED)
    _set_status(factory, item_ids[1], ProcessingStatus.FAILED)

    assert _confirm_snapshot(factory, "cluster", cluster_id) is False
    assert _statuses(factory)[item_ids[0]] == ProcessingStatus.NORMALIZED


def test_repository_refuses_singleton_that_became_cluster_member(factory):
    _source_id, item_ids = _add_items(factory, 2)
    summarizer, importance, dispatcher = _Summarizer(), _Importance(), _Dispatcher()
    _run(factory, summarizer=summarizer, importance=importance, dispatch=dispatcher)
    snapshot = _snapshot(factory, "feed_item", item_ids[0])
    _set_status(factory, item_ids[0], ProcessingStatus.NORMALIZED)
    _add_cluster(factory, item_ids)

    assert (
        _confirm(
            factory,
            kind="feed_item",
            unit_id=item_ids[0],
            expected_input_fingerprint=snapshot.input_fingerprint,
            expected_summary=snapshot.summary_result,
            expected_importance=snapshot.importance_result,
        )
        is False
    )
    assert _statuses(factory)[item_ids[0]] == ProcessingStatus.NORMALIZED


def test_repository_refuses_unknown_unit(factory):
    summary = _summary_result("Выжимка", _digest("вход"))
    assert (
        _confirm(
            factory,
            kind="feed_item",
            unit_id=999,
            expected_input_fingerprint="input-v1:" + "0" * 64,
            expected_summary=summary,
            expected_importance=_importance_result("Выжимка"),
        )
        is False
    )


def test_repository_refuses_results_from_another_run(factory):
    _source_id, item_ids = _add_items(factory, 1)
    summarizer, importance, dispatcher = _Summarizer(), _Importance(), _Dispatcher()
    _run(factory, summarizer=summarizer, importance=importance, dispatch=dispatcher)
    snapshot = _snapshot(factory, "feed_item", item_ids[0])
    _set_status(factory, item_ids[0], ProcessingStatus.NORMALIZED)

    assert (
        _confirm(
            factory,
            kind="feed_item",
            unit_id=item_ids[0],
            expected_input_fingerprint=snapshot.input_fingerprint,
            expected_summary=_summary_result(
                "Чужая выжимка", snapshot.summary_result.fingerprint
            ),
            expected_importance=snapshot.importance_result,
        )
        is False
    )
    assert (
        _confirm(
            factory,
            kind="feed_item",
            unit_id=item_ids[0],
            expected_input_fingerprint=snapshot.input_fingerprint,
            expected_summary=snapshot.summary_result,
            expected_importance=_importance_result("Чужая выжимка"),
        )
        is False
    )
    assert _statuses(factory)[item_ids[0]] == ProcessingStatus.NORMALIZED
    assert _confirm_snapshot(factory, "feed_item", item_ids[0]) is True
