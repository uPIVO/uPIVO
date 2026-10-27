"""Фоновые задачи персональной оценки по интересам.

Задачи не вызывают AI на уровне HTTP-запроса и не меняют глобальные записи
публикаций: результат пишется только в `user_item_scores` через guard-репозиторий.
Точка входа ITD-85 — `relevance.score_content_unit_for_eligible_users`.
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.services.user_relevance import UserRelevanceOutcome, build_default_service
from app.workers.celery_app import celery_app


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
