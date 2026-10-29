"""Общий проход по расписанию: порядок стадий, блокировка и безопасный AI.

Источники, стадии конвейера и AI заменены фейками: проверяется оркестрация
ITD-84, а не доступность внешних сервисов.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.pipeline import run as pipeline_run
from app.pipeline.lock import PipelineLockBusy, PipelineLockLost
from app.pipeline.run import (
    SKIP_BUDGET_CONTROLLER_UNAVAILABLE,
    SKIP_PAID_AI_DISABLED,
    run_shared_pipeline,
)
from app.processing.normalize import NormalizeStats
from app.services.source_sync import SourceSyncResult
from tests.fake_run_lock import FakeRunLock


@pytest.fixture
def factory():
    engine = create_engine("sqlite:///:memory:")
    yield sessionmaker(bind=engine)
    engine.dispose()


def _patch_stages(monkeypatch, *, pending: int = 1, normalize_failed: int = 0):
    order: list[str] = []
    ai_limits: list[int | None] = []

    def fake_normalize(session, *, limit=None, item_id=None, now=None):
        order.append("normalize")
        return NormalizeStats(processed=pending, failed=normalize_failed, skipped=0)

    def fake_extract(session):
        order.append("extract")
        return 0

    def fake_cluster(session, *, write):
        order.append("cluster")
        assert write is True
        return [], 0

    async def fake_analysis(*, session_factory, ai_limit):
        order.append("analyze")
        ai_limits.append(ai_limit)
        return pipeline_run.AnalysisStats()

    monkeypatch.setattr(pipeline_run, "normalize_pending", fake_normalize)
    monkeypatch.setattr(pipeline_run, "_extract_entities", fake_extract)
    monkeypatch.setattr(pipeline_run, "cluster_feed", fake_cluster)
    monkeypatch.setattr(pipeline_run, "run_common_analysis", fake_analysis)
    return order, ai_limits


def _patch_sync(monkeypatch, order: list[str], outcomes: list[SourceSyncResult]):
    calls: list[int | None] = []

    async def fake_sync_active_sources(*, session_factory, limit):
        order.append("sync")
        calls.append(limit)
        return outcomes

    monkeypatch.setattr(pipeline_run, "sync_active_sources", fake_sync_active_sources)
    return calls


def test_scheduled_pass_syncs_once_then_shares_global_stages(monkeypatch, factory):
    order, ai_limits = _patch_stages(monkeypatch)
    calls = _patch_sync(
        monkeypatch,
        order,
        [
            SourceSyncResult(10, "youtube", "collected", fetched=2, saved=2),
            SourceSyncResult(20, "reddit", "empty"),
        ],
    )
    lock = FakeRunLock()
    result = asyncio.run(
        run_shared_pipeline(session_factory=factory, limit=5, lock=lock)
    )

    assert calls == [5]
    assert order == ["sync", "normalize", "extract", "cluster"]
    assert ai_limits == []
    assert result.trigger == "schedule"
    assert result.analysis is None
    assert result.analysis_skip_reason == SKIP_PAID_AI_DISABLED
    assert result.normalize.processed == 1
    assert lock.acquired == 1
    assert lock.released == 1
    assert result.duration_seconds >= 0
    assert result.failed is False


def test_paid_flag_true_does_not_unlock_paid_ai(monkeypatch, factory):
    order, ai_limits = _patch_stages(monkeypatch)
    _patch_sync(monkeypatch, order, [SourceSyncResult(10, "youtube", "empty")])
    result = asyncio.run(
        run_shared_pipeline(
            session_factory=factory, allow_paid_ai=True, lock=FakeRunLock()
        )
    )

    assert order == ["sync", "normalize", "extract", "cluster"]
    assert ai_limits == []
    assert result.analysis is None
    assert result.analysis_skip_reason == SKIP_BUDGET_CONTROLLER_UNAVAILABLE
    payload = result.as_dict()
    assert payload["ai"] is None
    assert payload["ai_skipped_reason"] == SKIP_BUDGET_CONTROLLER_UNAVAILABLE


def test_partial_source_failure_keeps_other_results_and_stages(monkeypatch, factory):
    order, _ai_limits = _patch_stages(monkeypatch, pending=3)
    _patch_sync(
        monkeypatch,
        order,
        [
            SourceSyncResult(
                10, "youtube", "collected", fetched=4, saved=3, inserted=2, updated=1
            ),
            SourceSyncResult(20, "habr", "failed", reason="SourceFetchError"),
        ],
    )
    result = asyncio.run(run_shared_pipeline(session_factory=factory, lock=FakeRunLock()))

    assert order == ["sync", "normalize", "extract", "cluster"]
    assert result.normalize.processed == 3
    assert result.failed is True
    payload = json.loads(json.dumps(result.as_dict()))
    assert payload["status"] == "completed_with_failures"
    # Статусы и суммарные счётчики публикаций за проход.
    assert payload["sources"] == {
        "total": 2,
        "collected": 1,
        "empty": 0,
        "skipped": 0,
        "failed": 1,
        "fetched": 4,
        "saved": 3,
        "inserted": 2,
        "updated": 1,
    }
    assert payload["failed_sources"] == [
        {"source_id": 20, "type": "habr", "reason": "SourceFetchError"}
    ]
    assert payload["normalize"] == {"processed": 3, "failed": 0, "skipped": 0}
    assert payload["clusters"] == 0
    assert payload["unclustered"] == 0


def test_sources_without_insert_update_split_report_none(monkeypatch, factory):
    order, _ai_limits = _patch_stages(monkeypatch)
    _patch_sync(
        monkeypatch,
        order,
        [
            SourceSyncResult(10, "telegram", "collected", fetched=2, saved=2),
            SourceSyncResult(20, "reddit", "empty"),
        ],
    )
    result = asyncio.run(run_shared_pipeline(session_factory=factory, lock=FakeRunLock()))

    payload = result.as_dict()
    assert payload["sources"]["fetched"] == 2
    assert payload["sources"]["saved"] == 2
    # Telegram/Reddit не сообщают разбивку вставок/обновлений.
    assert payload["sources"]["inserted"] is None
    assert payload["sources"]["updated"] is None


def test_normalization_failure_marks_completed_with_failures(monkeypatch, factory):
    order, _ai_limits = _patch_stages(monkeypatch, pending=1, normalize_failed=2)
    _patch_sync(monkeypatch, order, [SourceSyncResult(10, "youtube", "empty")])

    result = asyncio.run(run_shared_pipeline(session_factory=factory, lock=FakeRunLock()))

    assert result.normalize.failed == 2
    assert result.failed is True
    payload = json.loads(json.dumps(result.as_dict()))
    assert payload["status"] == "completed_with_failures"
    assert payload["normalize"] == {"processed": 1, "failed": 2, "skipped": 0}


def test_manual_run_uses_the_same_global_stages(monkeypatch, factory):
    order, ai_limits = _patch_stages(monkeypatch)
    from app.pipeline.run import run_pipeline
    from app.services.collect import UserCollectResult

    async def fake_collect_for_user(identifier, *, session_factory, limit):
        order.append("collect")
        return UserCollectResult(
            user_id=1,
            email=identifier,
            outcomes=[SourceSyncResult(10, "youtube", "collected")],
        )

    monkeypatch.setattr(pipeline_run, "collect_for_user", fake_collect_for_user)
    result = asyncio.run(
        run_pipeline("one@example.com", session_factory=factory, lock=FakeRunLock())
    )

    assert order == ["collect", "normalize", "extract", "cluster", "analyze"]
    assert ai_limits == [50]
    assert result.analysis is not None
    assert result.run_id


def test_busy_lock_skips_scheduled_pass(monkeypatch, factory):
    order, _ai_limits = _patch_stages(monkeypatch)
    calls = _patch_sync(monkeypatch, order, [])
    lock = FakeRunLock(busy=True)

    with pytest.raises(PipelineLockBusy):
        asyncio.run(run_shared_pipeline(session_factory=factory, lock=lock))
    assert calls == []
    assert order == []
    assert lock.acquired == 1
    assert lock.released == 0


def test_lock_lost_between_stages_stops_without_reacquire(monkeypatch, factory):
    order, _ai_limits = _patch_stages(monkeypatch)
    _patch_sync(monkeypatch, order, [SourceSyncResult(10, "youtube", "collected")])
    lock = FakeRunLock(lost_after=1)

    with pytest.raises(PipelineLockLost):
        asyncio.run(run_shared_pipeline(session_factory=factory, lock=lock))
    # Сбор и коммит нормализации/склейки выполнены, дальше работа не продолжается.
    assert order == ["sync", "normalize", "extract", "cluster"]
    assert lock.acquired == 1
    assert lock.released == 1
    assert lock.verified == 2


def test_disconnected_lock_connection_aborts_without_reconnect(monkeypatch, factory):
    order, _ai_limits = _patch_stages(monkeypatch)
    _patch_sync(monkeypatch, order, [SourceSyncResult(10, "youtube", "collected")])
    lock = FakeRunLock()
    lock.live = False

    with pytest.raises(PipelineLockLost) as error:
        asyncio.run(run_shared_pipeline(session_factory=factory, lock=lock))
    assert "разорвано" in str(error.value)
    assert order == ["sync"]
    assert lock.acquired == 1
    assert lock.released == 1


def test_watchdog_cancels_async_stage_after_lock_loss():
    from app.pipeline.lock import run_with_lock_watchdog

    async def scenario():
        async def slow_stage():
            await asyncio.sleep(30)

        loop = asyncio.get_running_loop()
        started = loop.time()
        with pytest.raises(PipelineLockLost):
            await run_with_lock_watchdog(
                FakeRunLock(lost_after=0), slow_stage(), interval=0.01
            )
        return loop.time() - started

    assert asyncio.run(scenario()) < 5
