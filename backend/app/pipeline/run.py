"""Цепочка этапов: сбор → нормализация → сущности → склейка → общий AI-анализ.

Логика этапов не дублируется: AI-этап живёт в ``app.pipeline.analysis``.
Ручной запуск (``--user``) и запуск по расписанию (Celery, ITD-84) делят одни и
те же глобальные стадии через ``_run_global_stages``; различается только сбор:
ручной проход опрашивает источники одного пользователя, расписание — все
уникальные источники с хотя бы одной включённой подпиской.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.clustering.cluster import (
    ClusterView,
    StoryItem,
    build_plans,
    cluster_views,
    unclustered_count,
)
from app.clustering.persist import cluster_feed
from app.db.session import get_engine
from app.models.enums import ProcessingStatus
from app.models.feed_item import FeedItem
from app.models.source import Source
from app.models.user_source import UserSource
from app.pipeline.analysis import DEFAULT_AI_LIMIT, AnalysisStats, run_common_analysis
from app.pipeline.lock import (
    PipelineLockBusy,
    PostgresAdvisoryLock,
    RunLock,
    ensure_lock,
    run_with_lock_watchdog,
)
from app.processing.extractors.entity_extractor import apply_extracted_entities
from app.processing.normalize import (
    NormalizeStats,
    map_raw_payload,
    normalize_pending,
)
from app.services.collect import (
    UnknownUserError,
    UserCollectResult,
    collect_for_user,
    resolve_user,
)
from app.services.source_sync import SourceSyncResult, sync_active_sources

logger = logging.getLogger(__name__)

# Причина пропуска AI при явном opt-out автоматического платного этапа.
SKIP_PAID_AI_DISABLED = "paid_ai_disabled"


@dataclass
class PipelineResult:
    user_id: int
    email: str
    collect: UserCollectResult
    normalize: NormalizeStats
    clusters: list[ClusterView]
    unclustered: int
    dry_run: bool
    analysis: AnalysisStats | None = None
    run_id: str = ""


@dataclass(frozen=True)
class SharedPipelineResult:
    """Итоги общего прохода по расписанию (или другого общего запуска)."""

    run_id: str
    trigger: str
    sync: tuple[SourceSyncResult, ...]
    normalize: NormalizeStats
    clusters: list[ClusterView]
    unclustered: int
    analysis: AnalysisStats | None
    analysis_skip_reason: str | None
    duration_seconds: float

    @property
    def failed(self) -> bool:
        """Есть ли ошибки сбора, нормализации или AI за этот проход."""
        return (
            self.normalize.failed > 0
            or _sync_failed(self.sync)
            or (self.analysis is not None and self.analysis.has_failures)
        )

    def as_dict(self) -> dict[str, Any]:
        """Компактная сериализуемая статистика для результата Celery-задачи.

        ``sources`` содержит статусы источников и суммарные счётчики публикаций
        (``fetched``/``saved``/``inserted``/``updated``); ``None`` в
        ``inserted``/``updated`` означает, что источники такого разбиения не
        сообщают.
        """
        analysis = self.analysis
        return {
            "run_id": self.run_id,
            "trigger": self.trigger,
            "status": "completed_with_failures" if self.failed else "completed",
            "duration_seconds": round(self.duration_seconds, 3),
            "sources": _source_counts(self.sync),
            "failed_sources": [
                {
                    "source_id": row.source_id,
                    "type": row.source_type,
                    "reason": row.reason,
                }
                for row in self.sync
                if row.status == "failed"
            ],
            "normalize": {
                "processed": self.normalize.processed,
                "failed": self.normalize.failed,
                "skipped": self.normalize.skipped,
            },
            "clusters": len(self.clusters),
            "unclustered": self.unclustered,
            "ai": None
            if analysis is None
            else {
                "selected": analysis.selected,
                "succeeded": analysis.succeeded,
                "reused": analysis.reused,
                "failed": analysis.failed,
                "skipped": analysis.skipped,
                "queue_succeeded": analysis.queue_succeeded,
                "queue_failed": analysis.queue_failed,
                "receipt_failed": analysis.receipt_failed,
            },
            "ai_skipped_reason": self.analysis_skip_reason,
        }


@dataclass(frozen=True)
class FixturePipelineResult:
    processed: int
    failed: int
    clusters: list[ClusterView]
    unclustered: int


@dataclass(frozen=True)
class _GlobalStages:
    normalize: NormalizeStats
    clusters: list[ClusterView]
    unclustered: int
    analysis: AnalysisStats | None
    analysis_skip_reason: str | None


def chain_payloads(payloads: list[Any]) -> FixturePipelineResult:
    """Map raw payloads, then cluster the rows that mapped. No database."""
    stories: list[StoryItem] = []
    processed = 0
    failed = 0
    for payload in payloads:
        outcome = map_raw_payload(_unwrap_payload(payload))
        if not outcome.ok or outcome.content is None:
            failed += 1
            continue
        processed += 1
        mapped = outcome.content
        stories.append(
            StoryItem(
                id=processed,
                title=mapped.title,
                body_text=mapped.body_text,
                content_hash=mapped.content_hash,
                published_at=mapped.published_at,
            )
        )
    plans = build_plans(stories)
    return FixturePipelineResult(
        processed=processed,
        failed=failed,
        clusters=cluster_views(plans, stories),
        unclustered=unclustered_count(plans, stories),
    )


async def run_pipeline(
    identifier: str,
    *,
    session_factory: sessionmaker[Session] | None = None,
    limit: int | None = None,
    ai_limit: int | None = DEFAULT_AI_LIMIT,
    dry_run: bool = False,
    lock: RunLock | None = None,
) -> PipelineResult:
    """Собрать источники пользователя, нормализовать, склеить и запустить AI-этап.

    ``dry_run`` не опрашивает источники, откатывает нормализацию и склейку и не
    вызывает AI. ``ai_limit`` ограничивает число единиц общего AI-этапа за
    запуск; ``None`` — явный полный проход по backlog с соответствующей
    стоимостью. Вызывающая сторона не коммитит: функция коммитит сама, кроме
    ``dry_run``.

    Общий проход по расписанию может уже выполняться: тогда поднимается
    ``PipelineLockBusy``, а не запускается второй параллельный сбор. ``lock``
    подставляется тестами; в рабочем режиме блокировка берётся из PostgreSQL.
    """
    if limit is not None and limit < 1:
        raise ValueError("limit must be >= 1")
    if ai_limit is not None and ai_limit < 1:
        raise ValueError("ai_limit must be >= 1")
    factory = session_factory if session_factory is not None else sessionmaker(bind=get_engine())
    run_id = _new_run_id()
    run_lock = None if dry_run else _resolve_lock(lock, factory)
    if run_lock is not None and not run_lock.acquire():
        logger.info("Ручной запуск пропущен: skipped_locked run_id=%s", run_id)
        raise PipelineLockBusy("skipped_locked: полный общий проход уже выполняется")
    logger.info(
        "Ручной запуск конвейера: run_id=%s trigger=manual dry_run=%s", run_id, dry_run
    )
    started = time.monotonic()
    try:
        if dry_run:
            collected = _preview_sources(identifier, session_factory=factory)
        else:
            collected = await run_with_lock_watchdog(
                run_lock,
                collect_for_user(identifier, session_factory=factory, limit=limit),
            )
        stages = await _run_global_stages(
            session_factory=factory,
            limit=limit,
            ai_limit=ai_limit,
            dry_run=dry_run,
            run_analysis=not dry_run,
            analysis_skip_reason=None,
            lock=run_lock,
        )
    finally:
        if run_lock is not None:
            run_lock.release()
    result = PipelineResult(
        user_id=collected.user_id,
        email=collected.email,
        collect=collected,
        normalize=stages.normalize,
        clusters=stages.clusters,
        unclustered=stages.unclustered,
        dry_run=dry_run,
        analysis=stages.analysis,
        run_id=run_id,
    )
    logger.info(
        "Ручной запуск завершён: run_id=%s duration=%.3fs sources=%s failed_sources=%s "
        "normalized=%s failed=%s clusters=%s unclustered=%s ai_selected=%s ai_queue=%s",
        run_id,
        time.monotonic() - started,
        len(collected.outcomes),
        sum(row.status == "failed" for row in collected.outcomes),
        stages.normalize.processed,
        stages.normalize.failed,
        len(stages.clusters),
        stages.unclustered,
        None if stages.analysis is None else stages.analysis.selected,
        None if stages.analysis is None else stages.analysis.queue_succeeded,
    )
    return result


async def run_shared_pipeline(
    *,
    trigger: str = "schedule",
    session_factory: sessionmaker[Session] | None = None,
    limit: int | None = None,
    allow_paid_ai: bool = True,
    lock: RunLock | None = None,
) -> SharedPipelineResult:
    """Один общий проход: все активные источники, затем общие стадии конвейера.

    ``sync_active_sources`` вызывается ровно один раз за проход: каждый
    уникальный источник с хотя бы одной включённой подпиской опрашивается один
    раз, независимо от числа пользователей. Ошибка одного источника не
    прерывает остальные.

    По текущему решению продукта (2026-10-29) полный автоматический AI разрешён
    без контроллера расходов: автоматический проход вызывает существующий
    ``run_common_analysis`` с ``ai_limit=None`` — это весь накопившийся backlog,
    а не скрытые 50 единиц — и его штатную асинхронную постановку персональной
    оценки с квитанциями. Жёсткой денежной квоты нет; ``ai_limit`` остаётся
    техническим пределом, а не бюджетом. Учёт и контроль расходов — отдельная
    будущая работа. ``allow_paid_ai=False`` — осознанный opt-out: выполняются
    только бесплатные стадии, AI и постановка relevance не запускаются, причина
    пропуска — ``paid_ai_disabled``.
    """
    if limit is not None and limit < 1:
        raise ValueError("limit must be >= 1")
    factory = session_factory if session_factory is not None else sessionmaker(bind=get_engine())
    run_id = _new_run_id()
    run_lock = _resolve_lock(lock, factory)
    if not run_lock.acquire():
        logger.info("Общий проход пропущен: skipped_locked run_id=%s trigger=%s", run_id, trigger)
        raise PipelineLockBusy(f"skipped_locked: полный общий проход уже выполняется ({run_id})")
    started = time.monotonic()
    logger.info(
        "Запущен общий проход: run_id=%s trigger=%s paid_ai=%s", run_id, trigger, allow_paid_ai
    )
    try:
        outcomes = await run_with_lock_watchdog(
            run_lock, sync_active_sources(session_factory=factory, limit=limit)
        )
        stages = await _run_global_stages(
            session_factory=factory,
            limit=limit,
            ai_limit=None,
            dry_run=False,
            run_analysis=allow_paid_ai,
            analysis_skip_reason=None if allow_paid_ai else SKIP_PAID_AI_DISABLED,
            lock=run_lock,
        )
    finally:
        run_lock.release()
    result = SharedPipelineResult(
        run_id=run_id,
        trigger=trigger,
        sync=tuple(outcomes),
        normalize=stages.normalize,
        clusters=stages.clusters,
        unclustered=stages.unclustered,
        analysis=stages.analysis,
        analysis_skip_reason=stages.analysis_skip_reason,
        duration_seconds=time.monotonic() - started,
    )
    counts = _source_counts(result.sync)
    logger.info(
        "Общий проход завершён: run_id=%s trigger=%s duration=%.3fs sources=%s "
        "collected=%s empty=%s skipped=%s failed=%s fetched=%s saved=%s inserted=%s "
        "updated=%s normalized=%s normalize_failed=%s clusters=%s unclustered=%s "
        "ai_selected=%s queue_succeeded=%s queue_failed=%s ai_skipped_reason=%s",
        run_id,
        trigger,
        result.duration_seconds,
        counts["total"],
        counts["collected"],
        counts["empty"],
        counts["skipped"],
        counts["failed"],
        counts["fetched"],
        counts["saved"],
        counts["inserted"],
        counts["updated"],
        stages.normalize.processed,
        stages.normalize.failed,
        len(stages.clusters),
        stages.unclustered,
        None if stages.analysis is None else stages.analysis.selected,
        None if stages.analysis is None else stages.analysis.queue_succeeded,
        None if stages.analysis is None else stages.analysis.queue_failed,
        stages.analysis_skip_reason,
    )
    return result


async def _run_global_stages(
    *,
    session_factory: sessionmaker[Session],
    limit: int | None,
    ai_limit: int | None,
    dry_run: bool,
    run_analysis: bool,
    analysis_skip_reason: str | None,
    lock: RunLock | None,
) -> _GlobalStages:
    """Нормализация, сущности, склейка и общий AI-этап по общей БД."""
    ensure_lock(lock)
    stats, views, unclustered = _normalize_and_cluster(
        session_factory, limit=limit, dry_run=dry_run
    )
    if dry_run:
        return _GlobalStages(stats, views, unclustered, None, None)
    # Нормализация и кластеризация уже закоммичены: AI-этап видит готовые данные.
    ensure_lock(lock)
    if not run_analysis:
        return _GlobalStages(stats, views, unclustered, None, analysis_skip_reason)
    analysis = await run_with_lock_watchdog(
        lock, run_common_analysis(session_factory=session_factory, ai_limit=ai_limit)
    )
    ensure_lock(lock)
    return _GlobalStages(stats, views, unclustered, analysis, None)


def _resolve_lock(
    lock: RunLock | None, session_factory: sessionmaker[Session]
) -> RunLock:
    """Взять переданную блокировку или блокировку PostgreSQL по умолчанию.

    Отключения блокировки для не-PostgreSQL нет: тесты без PostgreSQL обязаны
    передать явную реализацию, иначе проход падает с понятной ошибкой.
    """
    if lock is not None:
        return lock
    return PostgresAdvisoryLock(_session_factory_engine(session_factory))


def _session_factory_engine(session_factory: sessionmaker[Session]) -> Engine:
    bind = getattr(session_factory, "kw", {}).get("bind")
    return bind if bind is not None else get_engine()


def _source_counts(
    outcomes: tuple[SourceSyncResult, ...] | list[SourceSyncResult],
) -> dict[str, int | None]:
    """Статусы источников и суммарные счётчики публикаций за проход."""
    inserted = [row.inserted for row in outcomes if row.inserted is not None]
    updated = [row.updated for row in outcomes if row.updated is not None]
    return {
        "total": len(outcomes),
        "collected": sum(row.status == "collected" for row in outcomes),
        "empty": sum(row.status == "empty" for row in outcomes),
        "skipped": sum(row.status == "skipped" for row in outcomes),
        "failed": sum(row.status == "failed" for row in outcomes),
        "fetched": sum(row.fetched for row in outcomes),
        "saved": sum(row.saved for row in outcomes),
        # None: эти источники не сообщают разбивку вставок/обновлений.
        "inserted": sum(inserted) if inserted else None,
        "updated": sum(updated) if updated else None,
    }


def _sync_failed(
    outcomes: tuple[SourceSyncResult, ...] | list[SourceSyncResult],
) -> bool:
    return any(row.status == "failed" for row in outcomes)


def _new_run_id() -> str:
    return uuid.uuid4().hex[:12]


def _preview_sources(
    identifier: str,
    *,
    session_factory: sessionmaker[Session],
) -> UserCollectResult:
    """List enabled sources without calling collectors."""
    with session_factory() as session:
        user = resolve_user(session, identifier)
        if user is None:
            raise UnknownUserError(identifier.strip() or identifier)
        user_id, email = user.id, user.email
        rows = session.execute(
            select(UserSource.source_id, Source.type)
            .join(Source, Source.id == UserSource.source_id)
            .where(UserSource.user_id == user_id, UserSource.enabled.is_(True))
            .order_by(UserSource.source_id)
        ).all()
    outcomes = [
        SourceSyncResult(
            source_id,
            source_type.value,
            "skipped",
            reason="dry-run",
        )
        for source_id, source_type in rows
    ]
    return UserCollectResult(user_id=user_id, email=email, outcomes=outcomes)


def _normalize_and_cluster(
    session_factory: sessionmaker[Session],
    *,
    limit: int | None,
    dry_run: bool,
) -> tuple[NormalizeStats, list[ClusterView], int]:
    with session_factory() as session:
        stats = normalize_pending(session, limit=limit)
        _extract_entities(session)
        views, unclustered = cluster_feed(session, write=not dry_run)
        if dry_run:
            session.rollback()
        else:
            # Общий AI-этап запускается после commit вызывающей стороной.
            session.commit()
        return stats, views, unclustered


def _extract_entities(session: Session) -> int:
    """Refresh deterministic entities for normalized shared publications."""
    rows = session.execute(
        select(FeedItem, Source.type)
        .join(Source, FeedItem.source_id == Source.id)
        .where(
            FeedItem.processing_status.in_(
                (ProcessingStatus.NORMALIZED, ProcessingStatus.PROCESSED)
            )
        )
        .order_by(FeedItem.id)
    ).all()
    changed = 0
    for item, source_type in rows:
        current = item.extracted_entities
        extracted = apply_extracted_entities(item, source_type)
        if extracted != current:
            changed += 1
    return changed


def _unwrap_payload(record: Any) -> Any:
    if isinstance(record, dict) and isinstance(record.get("raw_payload"), dict):
        return record["raw_payload"]
    return record
