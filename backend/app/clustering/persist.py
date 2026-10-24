"""Write story clusters. feed_items rows are left in place."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai.repository import ContentUnitAnalysisRepository
from app.clustering.cluster import (
    AssignedCluster,
    ClusterPlan,
    ClusterView,
    StoredCluster,
    StoryItem,
    assign_clusters,
    build_plans,
    cluster_views,
    stale_cluster_ids,
    unclustered_count,
)
from app.models.content_cluster import ClusterItem, ContentCluster
from app.models.enums import ProcessingStatus
from app.models.feed_item import FeedItem


def cluster_feed(session: Session, *, write: bool) -> tuple[list[ClusterView], int]:
    """Read feed_items, plan stories, optionally upsert content_clusters.

    Caller commits. A dry run passes write=False and should roll back.
    """
    items = _load_items(session)
    existing = _load_clusters(session)
    plans = build_plans(items)
    assigned = assign_clusters(existing, plans)
    if not write:
        cluster_ids = [slot.cluster_id for slot in assigned]
        return cluster_views(plans, items, cluster_ids), unclustered_count(plans, items)

    cluster_ids = _write(session, existing, assigned)
    return cluster_views(plans, items, cluster_ids), unclustered_count(plans, items)


def _load_items(session: Session) -> list[StoryItem]:
    rows = session.scalars(
        select(FeedItem)
        .where(
            FeedItem.processing_status.in_(
                (ProcessingStatus.NORMALIZED, ProcessingStatus.PROCESSED)
            )
        )
        .order_by(FeedItem.id)
    ).all()
    return [
        StoryItem(
            id=row.id,
            title=row.title,
            body_text=row.body_text,
            content_hash=row.content_hash,
            published_at=row.published_at,
        )
        for row in rows
    ]


def _load_clusters(session: Session) -> list[StoredCluster]:
    clusters = session.scalars(select(ContentCluster).order_by(ContentCluster.id)).all()
    links = session.scalars(select(ClusterItem)).all()
    members: dict[int, list[tuple[int, bool]]] = {cluster.id: [] for cluster in clusters}
    for link in links:
        members.setdefault(link.cluster_id, []).append((link.feed_item_id, link.is_primary))
    return [
        StoredCluster(
            id=cluster.id,
            title=cluster.title,
            summary=cluster.summary,
            members=tuple(sorted(members.get(cluster.id, []))),
        )
        for cluster in clusters
    ]


def _write(
    session: Session,
    existing: list[StoredCluster],
    assigned: list[AssignedCluster],
) -> list[int]:
    stale = sorted(stale_cluster_ids(existing, assigned))
    # Запись состава и AI-сохранение используют один lock родительского кластера.
    stale_rows = [
        session.get(ContentCluster, cluster_id, with_for_update=True, populate_existing=True)
        for cluster_id in stale
    ]
    if stale:
        links = session.scalars(select(ClusterItem).where(ClusterItem.cluster_id.in_(stale))).all()
        member_ids = sorted({link.feed_item_id for link in links})
        if member_ids:
            session.scalars(
                select(FeedItem)
                .where(FeedItem.id.in_(member_ids))
                .order_by(FeedItem.id)
                .with_for_update()
            ).all()
        for link in links:
            session.delete(link)
        session.flush()
        for row in stale_rows:
            if row is not None:
                session.delete(row)
        # Персональные результаты удаляются FK ON DELETE CASCADE.
        session.flush()

    repository = ContentUnitAnalysisRepository()
    cluster_ids: list[int] = []
    for slot in assigned:
        if slot.cluster_id is None:
            row = ContentCluster(title=slot.plan.title, summary=None)
            session.add(row)
            session.flush()
            cluster_id = row.id
        else:
            row = session.get(
                ContentCluster, slot.cluster_id, with_for_update=True, populate_existing=True
            )
            if row is None:
                raise RuntimeError(f"Кластер {slot.cluster_id} исчез во время сохранения.")
            if row.title != slot.plan.title:
                repository.invalidate_target(session, kind="cluster", unit_id=row.id)
                row.title = slot.plan.title
            cluster_id = slot.cluster_id
        _sync_members(session, cluster_id, slot.plan)
        cluster_ids.append(cluster_id)
    return cluster_ids


def _sync_members(session: Session, cluster_id: int, plan: ClusterPlan) -> None:
    links = session.scalars(select(ClusterItem).where(ClusterItem.cluster_id == cluster_id)).all()
    by_feed = {link.feed_item_id: link for link in links}
    desired = {feed_item_id: feed_item_id == plan.primary_id for feed_item_id in plan.member_ids}
    # Блокируем и старых, и новых участников до изменения членства. Одиночное
    # AI-сохранение блокирует те же FeedItem перед проверкой отсутствия кластера.
    session.scalars(
        select(FeedItem)
        .where(FeedItem.id.in_(sorted(set(by_feed) | set(desired))))
        .order_by(FeedItem.id)
        .with_for_update()
    ).all()
    if {item_id: link.is_primary for item_id, link in by_feed.items()} != desired:
        repository = ContentUnitAnalysisRepository()
        repository.invalidate_target(session, kind="cluster", unit_id=cluster_id)
        for item_id in sorted(set(desired) - set(by_feed)):
            repository.invalidate_target(session, kind="feed_item", unit_id=item_id)
    needs_flush = False
    for feed_item_id, link in by_feed.items():
        if feed_item_id not in desired:
            session.delete(link)
            needs_flush = True
            continue
        if link.is_primary and not desired[feed_item_id]:
            link.is_primary = False
            needs_flush = True
    # Clear the old primary before promoting another row under the partial unique index.
    if needs_flush:
        session.flush()
    for feed_item_id, link in by_feed.items():
        if feed_item_id in desired and desired[feed_item_id] and not link.is_primary:
            link.is_primary = True
    for feed_item_id, is_primary in desired.items():
        if feed_item_id not in by_feed:
            session.add(
                ClusterItem(
                    cluster_id=cluster_id,
                    feed_item_id=feed_item_id,
                    is_primary=is_primary,
                )
            )
