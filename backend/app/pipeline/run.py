"""Цепочка этапов: сбор → нормализация → склейка → общий AI-анализ.

Логика этапов не дублируется: AI-этап живёт в ``app.pipeline.analysis``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
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
from app.services.source_sync import SourceSyncResult


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


@dataclass(frozen=True)
class FixturePipelineResult:
    processed: int
    failed: int
    clusters: list[ClusterView]
    unclustered: int


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
) -> PipelineResult:
    """Collect one user's enabled sources, normalize, cluster and analyze.

    ``dry_run`` does not fetch sources, rolls back normalization and clustering
    and never calls AI. ``ai_limit`` bounds common AI units per run; ``None``
    means an explicit full pass over the backlog and costs accordingly.
    Caller does not commit; this function commits unless ``dry_run`` is set.
    """
    if limit is not None and limit < 1:
        raise ValueError("limit must be >= 1")
    if ai_limit is not None and ai_limit < 1:
        raise ValueError("ai_limit must be >= 1")
    factory = session_factory if session_factory is not None else sessionmaker(bind=get_engine())
    if dry_run:
        collected = _preview_sources(identifier, session_factory=factory)
    else:
        collected = await collect_for_user(identifier, session_factory=factory, limit=limit)
    stats, views, unclustered = _normalize_and_cluster(factory, limit=limit, dry_run=dry_run)
    analysis = (
        None if dry_run else await run_common_analysis(session_factory=factory, ai_limit=ai_limit)
    )
    return PipelineResult(
        user_id=collected.user_id,
        email=collected.email,
        collect=collected,
        normalize=stats,
        clusters=views,
        unclustered=unclustered,
        dry_run=dry_run,
        analysis=analysis,
    )


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
