"""Фоновые задачи: персональная оценка и общий сбор по расписанию.

`relevance.*` не вызывают AI на уровне HTTP-запроса и не меняют глобальные
записи публикаций: результат пишется только в `user_item_scores` через
guard-репозиторий. Точка входа ITD-85 —
`relevance.score_content_unit_for_eligible_users`.

`pipeline.run_scheduled` (ITD-84) — исключение из правила выше: один общий проход
по всем активным источникам нормализует и кластеризует общие публикации, но не
запускает автоматические платные AI-стадии.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.core.config import get_settings
from app.pipeline.lock import PipelineLockBusy
from app.pipeline.run import run_shared_pipeline
from app.services.user_relevance import UserRelevanceOutcome, build_default_service
from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)


def _outcome_json(outcome: UserRelevanceOutcome) -> dict[str, Any]:
    return {
        "user_id": outcome.user_id,
        "kind": outcome.kind,
        "unit_id": outcome.unit_id,
        "status": outcome.status.value,
        "attempts": outcome.attempts,
        "reused": outcome.reused,
        "error": outcome.error,
    }


@celery_app.task(name="relevance.score_user_content_unit")
def score_user_content_unit_task(user_id: int, kind: str, unit_id: int) -> dict[str, Any]:
    """Оценить одну единицу контента для одного пользователя."""
    outcome = asyncio.run(
        build_default_service().score_user_content_unit(user_id, kind, unit_id)
    )
    return _outcome_json(outcome)


@celery_app.task(name="relevance.score_content_unit_for_eligible_users")
def score_content_unit_for_eligible_users_task(kind: str, unit_id: int) -> list[int]:
    """Поставить персональные оценки для пользователей с доступом к единице."""
    return build_default_service().score_content_unit_for_eligible_users(kind, unit_id)


@celery_app.task(name="relevance.recalculate_user")
def recalculate_user_task(user_id: int) -> list[dict[str, Any]]:
    """Пересчитать только непросмотренные единицы пользователя."""
    selected = build_default_service().recalculate_unviewed_for_user(user_id)
    return [{"kind": ref.kind, "unit_id": ref.unit_id} for ref in selected]


@celery_app.task(name="relevance.recalculate_user_source")
def recalculate_user_source_task(user_id: int, source_id: int) -> list[dict[str, Any]]:
    """Пересчитать непросмотренное после активации одного источника."""
    selected = build_default_service().recalculate_unviewed_source_for_user(user_id, source_id)
    return [{"kind": ref.kind, "unit_id": ref.unit_id} for ref in selected]


@celery_app.task(name="pipeline.run_scheduled")
def run_scheduled_pipeline_task() -> dict[str, Any]:
    """Один общий проход по расписанию Beat: сбор и бесплатные стадии.

    Платный автоматический AI не запускается, пока нет проверенного контроллера
    расходов (ITD-84, раздел 4.1): сбор, нормализация, сущности и кластеризация
    выполняются полностью, а Summary/Importance/Relevance остаются за ручным
    явным запуском. Персональная relevance-задача здесь не ставится.

    Возвращается компактная сериализуемая статистика для результата задачи.
    Занятая блокировка — не ошибка: проход пропускается со статусом
    ``skipped_locked``. Потеря блокировки (``PipelineLockLost``) не повторяется
    автоматически и приводит к падению задачи, чтобы это было видно.
    """
    settings = get_settings()
    if not settings.scheduled_pipeline_enabled:
        logger.info(
            "Запуск по расписанию пропущен: skipped_disabled "
            "(SCHEDULED_PIPELINE_ENABLED=false)"
        )
        return {"status": "skipped_disabled", "trigger": "schedule"}
    try:
        result = asyncio.run(
            run_shared_pipeline(
                trigger="schedule",
                allow_paid_ai=settings.scheduled_paid_ai_enabled,
            )
        )
    except PipelineLockBusy as exc:
        logger.info("Запуск по расписанию пропущен: %s", exc)
        return {"status": "skipped_locked", "trigger": "schedule"}
    payload = result.as_dict()
    logger.info(
        "Результат запуска по расписанию: run_id=%s status=%s sources=%s failed_sources=%s",
        payload["run_id"],
        payload["status"],
        payload["sources"],
        payload["failed_sources"],
    )
    return payload
