"""Celery app проекта: брокер Redis, расписание общего сбора (ITD-84)."""

from __future__ import annotations

from typing import Any

from celery import Celery

from app.core.config import Settings, get_settings

SCHEDULED_TASK_NAME = "pipeline.run_scheduled"
SCHEDULED_TASK_KEY = "shared-pipeline"


def build_beat_schedule(settings: Settings | None = None) -> dict[str, dict[str, Any]]:
    """Расписание Beat: один общий проход с настраиваемым интервалом.

    Пустое расписание при ``SCHEDULED_PIPELINE_ENABLED=false``: Beat ничего не
    ставит, ручной CLI и персональные relevance-задачи работают как раньше.
    Часовой пояс Celery — UTC. Beat только отправляет задачу брокеру, исполняет
    её Worker; один экземпляр Beat на расписание, иначе постановки дублируются.
    """
    conf = settings if settings is not None else get_settings()
    if not conf.scheduled_pipeline_enabled:
        return {}
    return {
        SCHEDULED_TASK_KEY: {
            "task": SCHEDULED_TASK_NAME,
            "schedule": float(conf.scheduled_pipeline_interval_seconds),
        }
    }


celery_app = Celery(
    "upivo",
    broker=get_settings().redis_url.get_secret_value(),
    include=["app.workers.tasks"],
)
celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    beat_schedule=build_beat_schedule(),
    broker_connection_retry_on_startup=True,
)
