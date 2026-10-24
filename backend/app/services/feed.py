"""Service for generating personalized user news feed and detailed item view with cluster support."""

import logging
from sqlalchemy import String, cast, func, or_, select
from sqlalchemy.orm import Session

from app.models.content_cluster import ClusterItem, ContentCluster
from app.models.enums import SourceType
from app.models.feed_item import FeedItem
from app.models.source import Source
from app.models.user_source import UserSource
from app.schemas.feed import (
    FeedItemDetail,
    FeedItemListItem,
    PaginatedFeedResponse,
    RelatedFeedItem,
    SourceBrief,
)

logger = logging.getLogger(__name__)


class FeedService:
    def __init__(self, db: Session):
        self.db = db

    def get_user_feed(
        self,
        user_id: int,
        source_type: SourceType | None = None,
        topic: str | None = None,
        limit: int = 20,
        offset: int = 0,
        summary_fallback_length: int = 250,
    ) -> PaginatedFeedResponse:
        """
        Formulate a user-specific news feed:
        1. Select feed items from sources subscribed by the user (UserSource.enabled = true).
        2. Optional filter by source_type (e.g. habr, reddit, telegram, youtube).
        3. Optional filter by topic / tag (searches in title, body, summary, or raw_payload tags/hubs).
        4. Sort by publication date descending (published_at DESC, collected_at DESC).
        5. Group publications belonging to the same cluster into ONE card (story).
        6. Return items with title formatted in uppercase, summary, publication date, sources info, and link.
        """
        # Base query joining FeedItem, Source, UserSource, ClusterItem, and ContentCluster
        base_query = (
            select(FeedItem, Source, ClusterItem, ContentCluster)
            .join(Source, FeedItem.source_id == Source.id)
            .join(
                UserSource,
                (UserSource.source_id == Source.id)
                & (UserSource.user_id == user_id)
                & (UserSource.enabled.is_(True)),
            )
            .outerjoin(ClusterItem, ClusterItem.feed_item_id == FeedItem.id)
            .outerjoin(ContentCluster, ContentCluster.id == ClusterItem.cluster_id)
        )

        if source_type:
            base_query = base_query.where(Source.type == source_type)

        if topic and topic.strip():
            clean_topic = topic.strip()
            pattern = f"%{clean_topic}%"

            # Search in title, summary, body_text or JSON tags/hubs in raw_payload, or cluster title/summary
            raw_payload_text = cast(FeedItem.raw_payload, String)
            topic_filter = or_(
                FeedItem.title.ilike(pattern),
                FeedItem.summary.ilike(pattern),
                FeedItem.body_text.ilike(pattern),
                raw_payload_text.ilike(pattern),
                ContentCluster.title.ilike(pattern),
                ContentCluster.summary.ilike(pattern),
            )
            base_query = base_query.where(topic_filter)

        # Order candidates by newest published first
        items_query = base_query.order_by(
            FeedItem.published_at.desc().nulls_last(),
            FeedItem.collected_at.desc(),
            FeedItem.id.desc(),
        )

        rows = self.db.execute(items_query).all()

        # Step 1: Collect items, grouping by cluster_id while preserving appearance order
        cards_order: list[int | tuple[str, int]] = []  # cluster_id or ('item', feed_item_id)
        cluster_groups: dict[int, list[tuple[FeedItem, Source, ClusterItem | None, ContentCluster | None]]] = {}
        standalone_items: dict[int, tuple[FeedItem, Source]] = {}

        for feed_item, source, cluster_item, cluster in rows:
            cluster_id = cluster_item.cluster_id if cluster_item else None
            if cluster_id is not None:
                if cluster_id not in cluster_groups:
                    cluster_groups[cluster_id] = []
                    cards_order.append(cluster_id)
                cluster_groups[cluster_id].append((feed_item, source, cluster_item, cluster))
            else:
                key = ("item", feed_item.id)
                standalone_items[feed_item.id] = (feed_item, source)
                cards_order.append(key)

        # Step 2: Build final card objects
        cards: list[FeedItemListItem] = []
        for key in cards_order:
            if isinstance(key, tuple):
                _, item_id = key
                feed_item, source = standalone_items[item_id]
                source_brief = SourceBrief(id=source.id, name=source.name, type=source.type)

                title_text = (feed_item.title or "БЕЗ НАЗВАНИЯ").strip().upper()
                summary_text = feed_item.summary
                if not summary_text:
                    raw_body = (feed_item.body_text or "").strip()
                    summary_text = raw_body[:summary_fallback_length].rstrip() + "..." if len(raw_body) > summary_fallback_length else raw_body

                cards.append(
                    FeedItemListItem(
                        id=feed_item.id,
                        title=title_text,
                        summary=summary_text,
                        published_at=feed_item.published_at,
                        source=source_brief,
                        original_url=feed_item.original_url,
                        cluster_id=None,
                        is_cluster=False,
                        publications_count=1,
                        sources=[source_brief],
                        related_items=[],
                    )
                )
            else:
                cluster_id = key
                group = cluster_groups[cluster_id]
                cluster = group[0][3]  # ContentCluster model if present

                # Find primary publication in this cluster (or pick first/latest)
                primary_tuple = next((t for t in group if t[2] and t[2].is_primary), group[0])
                primary_item, primary_source, _, _ = primary_tuple
                primary_source_brief = SourceBrief(
                    id=primary_source.id,
                    name=primary_source.name,
                    type=primary_source.type,
                )

                # Title & Summary (prefer cluster-level AI synthesis, fallback to primary item)
                if cluster and cluster.title:
                    title_text = cluster.title.strip().upper()
                else:
                    title_text = (primary_item.title or "БЕЗ НАЗВАНИЯ").strip().upper()

                if cluster and cluster.summary:
                    summary_text = cluster.summary
                elif primary_item.summary:
                    summary_text = primary_item.summary
                else:
                    raw_body = (primary_item.body_text or "").strip()
                    summary_text = raw_body[:summary_fallback_length].rstrip() + "..." if len(raw_body) > summary_fallback_length else raw_body

                # Unique sources across all publications in the cluster
                unique_sources: list[SourceBrief] = []
                seen_source_ids = set()
                for _, s, _, _ in group:
                    if s.id not in seen_source_ids:
                        seen_source_ids.add(s.id)
                        unique_sources.append(SourceBrief(id=s.id, name=s.name, type=s.type))

                # Related items (all member publications except the primary one)
                related_items: list[RelatedFeedItem] = [
                    RelatedFeedItem(
                        id=item.id,
                        title=item.title or "Без названия",
                        source=SourceBrief(id=s.id, name=s.name, type=s.type),
                        original_url=item.original_url,
                        published_at=item.published_at,
                    )
                    for item, s, _, _ in group
                    if item.id != primary_item.id
                ]

                # Most recent publication date in cluster
                max_published_at = max(
                    (item.published_at for item, _, _, _ in group if item.published_at is not None),
                    default=primary_item.published_at,
                )

                cards.append(
                    FeedItemListItem(
                        id=primary_item.id,
                        title=title_text,
                        summary=summary_text,
                        published_at=max_published_at,
                        source=primary_source_brief,
                        original_url=primary_item.original_url,
                        cluster_id=cluster_id,
                        is_cluster=len(group) > 1,
                        publications_count=len(group),
                        sources=unique_sources,
                        related_items=related_items,
                    )
                )

        total = len(cards)
        paginated_items = cards[offset : offset + limit]

        return PaginatedFeedResponse(
            items=paginated_items,
            total=total,
            limit=limit,
            offset=offset,
        )

    def get_feed_item_detail(self, item_id: int) -> FeedItemDetail | None:
        """Retrieve full post details by item ID, including cluster-related publications."""
        query = (
            select(FeedItem, Source, ClusterItem)
            .join(Source, FeedItem.source_id == Source.id)
            .outerjoin(ClusterItem, ClusterItem.feed_item_id == FeedItem.id)
            .where(FeedItem.id == item_id)
        )
        row = self.db.execute(query).first()
        if not row:
            return None

        feed_item, source, cluster_item = row

        related_publications: list[RelatedFeedItem] = []
        cluster_id = cluster_item.cluster_id if cluster_item else None

        if cluster_id is not None:
            # Find other items in the same cluster
            other_items_query = (
                select(FeedItem, Source)
                .join(Source, FeedItem.source_id == Source.id)
                .join(ClusterItem, ClusterItem.feed_item_id == FeedItem.id)
                .where(
                    ClusterItem.cluster_id == cluster_id,
                    FeedItem.id != item_id,
                )
                .order_by(FeedItem.published_at.desc().nulls_last())
            )
            other_rows = self.db.execute(other_items_query).all()
            for other_item, other_source in other_rows:
                related_publications.append(
                    RelatedFeedItem(
                        id=other_item.id,
                        title=other_item.title or "Без названия",
                        source=SourceBrief(
                            id=other_source.id,
                            name=other_source.name,
                            type=other_source.type,
                        ),
                        original_url=other_item.original_url,
                        published_at=other_item.published_at,
                    )
                )

        # Extract tags/hubs from raw_payload if available
        tags: list[str] = []
        if feed_item.raw_payload and isinstance(feed_item.raw_payload, dict):
            tags.extend(feed_item.raw_payload.get("tags") or [])
            tags.extend(feed_item.raw_payload.get("hubs") or [])

        return FeedItemDetail(
            id=feed_item.id,
            title=feed_item.title or "Без названия",
            summary=feed_item.summary,
            body_text=feed_item.body_text,
            author=feed_item.author,
            published_at=feed_item.published_at,
            original_url=feed_item.original_url,
            source=SourceBrief(
                id=source.id,
                name=source.name,
                type=source.type,
            ),
            tags=list(dict.fromkeys(tags)),  # Deduplicate while preserving order
            extracted_entities=feed_item.extracted_entities,
            cluster_id=cluster_id,
            related_publications=related_publications,
        )
