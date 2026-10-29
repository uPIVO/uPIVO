"""Celery-задача общего запуска: регистрация, расписание, безопасный AI.

Beat и Worker не поднимаются: проверяется постановка расписания, регистрация
задачи рядом с relevance-задачами и компактный сериализуемый результат.
Реальная передача через Redis/PostgreSQL проверяется Compose-прогоном.
"""

from __future__ import annotations

import asyncio
import json

from app.core.config import Settings
from app.pipeline.lock import PipelineLockBusy
from app.pipeline.run import SharedPipelineResult
from app.processing.normalize import NormalizeStats
from app.services.source_sync import SourceSyncResult
from app.workers import tasks
from app.workers.celery_app import (
    SCHEDULED_TASK_NAME,
    build_beat_schedule,
    celery_app,
)


def _settings(**overrides) -> Settings:
    values = {
        "scheduled_pipeline_enabled": True,
        "scheduled_pipeline_interval_seconds": 1800,
        "scheduled_paid_ai_enabled": False,
    }
    values.update(overrides)
    return Settings(**values)


def _result(**overrides) -> SharedPipelineResult:
    values = {
        "run_id": "run123",
        "trigger": "schedule",
        "sync": (SourceSyncResult(10, "youtube", "collected", fetched=2, saved=2),),
        "normalize": NormalizeStats(processed=2, failed=0, skipped=0),
        "clusters": [],
        "unclustered": 2,
        "analysis": None,
        "analysis_skip_reason": "paid_ai_disabled",
        "duration_seconds": 1.23456,
    }
    values.update(overrides)
    return SharedPipelineResult(**values)


def test_beat_schedule_is_empty_by_default():
    assert build_beat_schedule(Settings(scheduled_pipeline_enabled=False)) == {}
    assert celery_app.conf.beat_schedule == build_beat_schedule()


def test_beat_schedule_uses_configured_interval_in_utc():
    schedule = build_beat_schedule(_settings())
    assert schedule == {
        "shared-pipeline": {
            "task": SCHEDULED_TASK_NAME,
            "schedule": 1800.0,
        }
    }
    assert build_beat_schedule(_settings(scheduled_pipeline_interval_seconds=120))[
        "shared-pipeline"
    ]["schedule"] == 120.0
    assert celery_app.conf.timezone == "UTC"
    assert celery_app.conf.enable_utc is True


def test_scheduled_task_is_registered_next_to_relevance_tasks():
    assert SCHEDULED_TASK_NAME in celery_app.tasks
    for name in (
        "relevance.score_user_content_unit",
        "relevance.score_content_unit_for_eligible_users",
        "relevance.recalculate_user",
        "relevance.recalculate_user_source",
    ):
        assert name in celery_app.tasks


def test_task_skips_when_schedule_disabled(monkeypatch):
    called: list[dict] = []

    async def fake_run(**kwargs):
        called.append(kwargs)
        return _result()

    monkeypatch.setattr(tasks, "run_shared_pipeline", fake_run)
    monkeypatch.setattr(tasks, "get_settings", lambda: _settings(scheduled_pipeline_enabled=False))

    assert tasks.run_scheduled_pipeline_task() == {
        "status": "skipped_disabled",
        "trigger": "schedule",
    }
    assert called == []


def test_task_returns_skipped_locked_without_running_stages(monkeypatch):
    async def fake_run(**kwargs):
        raise PipelineLockBusy("skipped_locked")

    monkeypatch.setattr(tasks, "run_shared_pipeline", fake_run)
    monkeypatch.setattr(tasks, "get_settings", lambda: _settings())

    assert tasks.run_scheduled_pipeline_task() == {
        "status": "skipped_locked",
        "trigger": "schedule",
    }


def test_task_runs_shared_pipeline_once_and_returns_json(monkeypatch):
    calls: list[dict] = []

    async def fake_run(**kwargs):
        calls.append(kwargs)
        return _result()

    monkeypatch.setattr(tasks, "run_shared_pipeline", fake_run)
    monkeypatch.setattr(tasks, "get_settings", lambda: _settings())

    payload = tasks.run_scheduled_pipeline_task()

    assert calls == [{"trigger": "schedule", "allow_paid_ai": False}]
    encoded = json.dumps(payload)
    assert len(encoded) < 2000
    assert payload["status"] == "completed"
    assert payload["sources"]["collected"] == 1
    assert payload["ai"] is None
    assert payload["ai_skipped_reason"] == "paid_ai_disabled"
    assert payload["duration_seconds"] == 1.235


def test_task_passes_paid_flag_through_without_unlocking_ai(monkeypatch):
    calls: list[dict] = []

    async def fake_run(**kwargs):
        calls.append(kwargs)
        return _result(analysis_skip_reason="budget_controller_unavailable")

    monkeypatch.setattr(tasks, "run_shared_pipeline", fake_run)
    monkeypatch.setattr(tasks, "get_settings", lambda: _settings(scheduled_paid_ai_enabled=True))

    payload = tasks.run_scheduled_pipeline_task()

    assert calls == [{"trigger": "schedule", "allow_paid_ai": True}]
    assert payload["ai"] is None
    assert payload["ai_skipped_reason"] == "budget_controller_unavailable"


def test_task_wrapper_is_synchronous_and_uses_asyncio_run(monkeypatch):
    async def fake_run(**kwargs):
        await asyncio.sleep(0)
        return _result()

    monkeypatch.setattr(tasks, "run_shared_pipeline", fake_run)
    monkeypatch.setattr(tasks, "get_settings", lambda: _settings())

    assert not asyncio.iscoroutinefunction(tasks.run_scheduled_pipeline_task)
    assert tasks.run_scheduled_pipeline_task()["run_id"] == "run123"
