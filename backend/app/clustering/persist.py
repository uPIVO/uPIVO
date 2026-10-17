"""Write story clusters. feed_items rows are left in place."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

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
    rows = session.scalars(select(FeedItem).order_by(FeedItem.id)).all()
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
    stale = stale_cluster_ids(existing, assigned)
    if stale:
        links = session.scalars(select(ClusterItem).where(ClusterItem.cluster_id.in_(stale))).all()
        for link in links:
            session.delete(link)
        session.flush()
        for cluster_id in stale:
            row = session.get(ContentCluster, cluster_id)
            if row is not None:
                session.delete(row)
        session.flush()

    cluster_ids: list[int] = []
    for slot in assigned:
        if slot.cluster_id is None:
            row = ContentCluster(title=slot.plan.title, summary=None)
            session.add(row)
            session.flush()
            cluster_id = row.id
        else:
            row = session.get(ContentCluster, slot.cluster_id)
            if row is None:
                raise RuntimeError(f"content_clusters.id {slot.cluster_id} disappeared")
            row.title = slot.plan.title
            cluster_id = slot.cluster_id
        _sync_members(session, cluster_id, slot.plan)
        cluster_ids.append(cluster_id)
    return cluster_ids


def _sync_members(session: Session, cluster_id: int, plan: ClusterPlan) -> None:
    links = session.scalars(select(ClusterItem).where(ClusterItem.cluster_id == cluster_id)).all()
    by_feed = {link.feed_item_id: link for link in links}
    desired = {feed_item_id: feed_item_id == plan.primary_id for feed_item_id in plan.member_ids}
    for feed_item_id, link in by_feed.items():
        if feed_item_id not in desired:
            session.delete(link)
            continue
        if link.is_primary != desired[feed_item_id]:
            link.is_primary = desired[feed_item_id]
    for feed_item_id, is_primary in desired.items():
        if feed_item_id not in by_feed:
            session.add(
                ClusterItem(
                    cluster_id=cluster_id,
                    feed_item_id=feed_item_id,
                    is_primary=is_primary,
                )
            )
