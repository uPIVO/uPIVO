"""Постановка фоновых задач персональной оценки в Celery."""

from __future__ import annotations

from app.workers.celery_app import celery_app


class CeleryTaskScheduler:
    """Планировщик фоновых работ персональной релевантности.

    HTTP-запрос и изменение интересов только ставят задачи: AI-вызовы
    выполняет worker, а не синхронная обработка запроса.
    """

    def schedule_content_unit(self, *, user_id: int, kind: str, unit_id: int) -> None:
        celery_app.send_task(
            "relevance.score_user_content_unit",
            kwargs={"user_id": user_id, "kind": kind, "unit_id": unit_id},
        )

    def schedule_user_recalculation(self, *, user_id: int) -> None:
        celery_app.send_task(
            "relevance.recalculate_user",
            kwargs={"user_id": user_id},
        )

    def schedule_source_recalculation(self, *, user_id: int, source_id: int) -> None:
        celery_app.send_task(
            "relevance.recalculate_user_source",
            kwargs={"user_id": user_id, "source_id": source_id},
        )
