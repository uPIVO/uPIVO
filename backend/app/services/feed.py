"""Service for generating personalized user news feed and detailed item view."""

import logging
from datetime import UTC, datetime

from sqlalchemy import String, cast, or_, select
from sqlalchemy.orm import Session

from app.models.content_cluster import ClusterItem, ContentCluster
from app.models.enums import ProcessingStatus, SourceType
from app.models.feed_item import FeedItem
from app.models.source import Source
from app.models.user_item_score import UserItemScore
from app.models.user_source import UserSource
from app.schemas.feed import (
    FeedItemDetail,
    FeedItemListItem,
    PaginatedFeedResponse,
    SourceBrief,
)
from app.services.ranking import calculate_card_rank
from app.services.users import require_user

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
        3. Optional topic/tag filter (searches title, body, summary, raw_payload tags/hubs).
        4. Group publications of the same story (ContentCluster) into a single card.
        5. Rank cards by relevance (user_item_scores), importance, and freshness.
        6. Return original title/summary/source or cluster-aggregated card.
        """
        require_user(self.db, user_id)
        # Base query joining FeedItem, Source, and UserSource
        base_query = (
            select(
                FeedItem,
                Source,
                ClusterItem.cluster_id,
                ClusterItem.is_primary,
                ContentCluster.title.label("cluster_title"),
                ContentCluster.summary.label("cluster_summary"),
                ContentCluster.importance_score.label("cluster_importance_score"),
            )
            .join(Source, FeedItem.source_id == Source.id)
            .join(
                UserSource,
                (UserSource.source_id == Source.id)
                & (UserSource.user_id == user_id)
                & (UserSource.enabled.is_(True)),
            )
            .outerjoin(ClusterItem, ClusterItem.feed_item_id == FeedItem.id)
            .outerjoin(ContentCluster, ContentCluster.id == ClusterItem.cluster_id)
            .where(
                FeedItem.processing_status.in_(
                    (ProcessingStatus.NORMALIZED, ProcessingStatus.PROCESSED)
                )
            )
        )

        if source_type:
            base_query = base_query.where(Source.type == source_type)

        if topic and topic.strip():
            clean_topic = topic.strip()
            pattern = f"%{clean_topic}%"

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

        # Order candidates chronologically
        ordered_query = base_query.order_by(
            FeedItem.published_at.desc().nulls_last(),
            FeedItem.collected_at.desc(),
            FeedItem.id.desc(),
        )

        all_matching_rows = self.db.execute(ordered_query).all()

        # Group multiple publications of the same cluster into a single story
        # Key: ("cluster", cluster_id) or ("item", feed_item_id)
        cards: list[dict] = []
        cluster_index_map: dict[int, int] = {}

        for row in all_matching_rows:
            (
                feed_item,
                source,
                cluster_id,
                is_primary,
                cluster_title,
                cluster_summary,
                cluster_importance,
            ) = row

            if cluster_id is not None:
                if cluster_id in cluster_index_map:
                    # Append secondary publication to existing cluster card
                    card = cards[cluster_index_map[cluster_id]]
                    card["feed_items"].append((feed_item, source, bool(is_primary)))
                    if is_primary:
                        card["primary_item"] = feed_item
                        card["primary_source"] = source
                else:
                    cluster_index_map[cluster_id] = len(cards)
                    cards.append(
                        {
                            "is_cluster": True,
                            "cluster_id": cluster_id,
                            "cluster_title": cluster_title,
                            "cluster_summary": cluster_summary,
                            "cluster_importance": cluster_importance,
                            "primary_item": feed_item,
                            "primary_source": source,
                            "feed_items": [(feed_item, source, bool(is_primary))],
                        }
                    )
            else:
                cards.append(
                    {
                        "is_cluster": False,
                        "cluster_id": None,
                        "cluster_title": None,
                        "cluster_summary": None,
                        "cluster_importance": None,
                        "primary_item": feed_item,
                        "primary_source": source,
                        "feed_items": [(feed_item, source, False)],
                    }
                )

        # Load user_item_scores for candidate items and clusters
        candidate_feed_ids = [row[0].id for row in all_matching_rows]
        candidate_cluster_ids = [row[2] for row in all_matching_rows if row[2] is not None]

        user_scores_by_feed: dict[int, float] = {}
        user_scores_by_cluster: dict[int, float] = {}

        if candidate_feed_ids or candidate_cluster_ids:
            score_filters = []
            if candidate_feed_ids:
                score_filters.append(UserItemScore.feed_item_id.in_(candidate_feed_ids))
            if candidate_cluster_ids:
                score_filters.append(UserItemScore.cluster_id.in_(candidate_cluster_ids))

            scores_query = select(UserItemScore).where(
                UserItemScore.user_id == user_id,
                or_(*score_filters),
            )
            for sc in self.db.execute(scores_query).scalars().all():
                if sc.feed_item_id and sc.relevance_score is not None:
                    user_scores_by_feed[sc.feed_item_id] = sc.relevance_score
                if sc.cluster_id and sc.relevance_score is not None:
                    user_scores_by_cluster[sc.cluster_id] = sc.relevance_score

        now = datetime.now(UTC)

        # Compute rank_score for each card and sort: rank_score DESC, published_at DESC, id DESC
        for card in cards:
            # If it's a cluster, ensure primary_item is properly resolved
            if card["is_cluster"]:
                for f_item, src, is_prim in card["feed_items"]:
                    if is_prim:
                        card["primary_item"] = f_item
                        card["primary_source"] = src
                        break

            rank = calculate_card_rank(
                is_cluster=card["is_cluster"],
                cluster_id=card["cluster_id"],
                primary_item=card["primary_item"],
                feed_items=card["feed_items"],
                cluster_importance=card.get("cluster_importance"),
                user_scores_by_feed=user_scores_by_feed,
                user_scores_by_cluster=user_scores_by_cluster,
                now=now,
            )
            card["rank_score"] = rank

        # Sort cards: highest rank first; break ties by publication time and ID
        def _card_sort_key(c: dict) -> tuple:
            p_item = c["primary_item"]
            pub = p_item.published_at or p_item.collected_at or datetime.min
            return (c["rank_score"], pub, p_item.id)

        cards.sort(key=_card_sort_key, reverse=True)

        total_cards = len(cards)
        paginated_cards = cards[offset : offset + limit]

        feed_items: list[FeedItemListItem] = []
        for card in paginated_cards:
            primary_item: FeedItem = card["primary_item"]
            primary_source: Source = card["primary_source"]

            # If it's a cluster, prefer explicit is_primary item if found
            if card["is_cluster"]:
                for f_item, src, is_prim in card["feed_items"]:
                    if is_prim:
                        primary_item = f_item
                        primary_source = src
                        break

            # Title
            if card["is_cluster"] and card["cluster_title"]:
                title_text = card["cluster_title"].strip()
            else:
                title_text = (primary_item.title or "Без названия").strip()

            # Summary
            if card["is_cluster"] and card["cluster_summary"]:
                summary_text = card["cluster_summary"].strip()
            else:
                summary_text = primary_item.summary
                if not summary_text:
                    raw_body = (primary_item.body_text or "").strip()
                    if len(raw_body) > summary_fallback_length:
                        summary_text = raw_body[:summary_fallback_length].rstrip() + "..."
                    else:
                        summary_text = raw_body

            # Related sources list
            seen_source_ids = set()
            related_sources: list[SourceBrief] = []
            for _, src, _ in card["feed_items"]:
                if src.id not in seen_source_ids:
                    seen_source_ids.add(src.id)
                    related_sources.append(
                        SourceBrief(
                            id=src.id,
                            name=src.name,
                            type=src.type,
                        )
                    )

            feed_items.append(
                FeedItemListItem(
                    id=primary_item.id,
                    title=title_text,
                    summary=summary_text,
                    published_at=primary_item.published_at,
                    source=SourceBrief(
                        id=primary_source.id,
                        name=primary_source.name,
                        type=primary_source.type,
                    ),
                    original_url=primary_item.original_url,
                    is_cluster=card["is_cluster"],
                    cluster_id=card["cluster_id"],
                    items_count=len(card["feed_items"]),
                    related_sources=related_sources,
                )
            )

        return PaginatedFeedResponse(
            items=feed_items,
            total=total_cards,
            limit=limit,
            offset=offset,
        )

    def get_feed_item_detail(self, user_id: int, item_id: int) -> FeedItemDetail | None:
        """Retrieve details for an item (or its cluster if it belongs to one)."""
        require_user(self.db, user_id)
        query = (
            select(FeedItem, Source, ClusterItem.cluster_id, ContentCluster)
            .join(Source, FeedItem.source_id == Source.id)
            .join(
                UserSource,
                (UserSource.source_id == Source.id)
                & (UserSource.user_id == user_id)
                & (UserSource.enabled.is_(True)),
            )
            .outerjoin(ClusterItem, ClusterItem.feed_item_id == FeedItem.id)
            .outerjoin(ContentCluster, ContentCluster.id == ClusterItem.cluster_id)
            .where(
                FeedItem.id == item_id,
                FeedItem.processing_status.in_(
                    (ProcessingStatus.NORMALIZED, ProcessingStatus.PROCESSED)
                ),
            )
        )
        row = self.db.execute(query).first()
        if not row:
            return None

        feed_item, source, cluster_id, cluster = row

        # Extract tags/hubs from raw_payload if available
        tags: list[str] = []
        if feed_item.raw_payload and isinstance(feed_item.raw_payload, dict):
            tags.extend(feed_item.raw_payload.get("tags") or [])
            tags.extend(feed_item.raw_payload.get("hubs") or [])

        # Related items if part of a cluster
        related_items: list[FeedItemListItem] = []
        is_cluster = False
        items_count = 1
        title = feed_item.title or "Без названия"
        summary = feed_item.summary

        if cluster_id is not None:
            is_cluster = True
            if cluster and cluster.title:
                title = cluster.title
            if cluster and cluster.summary:
                summary = cluster.summary

            # Load all member items of this cluster for this user
            cluster_members_query = (
                select(FeedItem, Source)
                .join(ClusterItem, ClusterItem.feed_item_id == FeedItem.id)
                .join(Source, FeedItem.source_id == Source.id)
                .join(
                    UserSource,
                    (UserSource.source_id == Source.id)
                    & (UserSource.user_id == user_id)
                    & (UserSource.enabled.is_(True)),
                )
                .where(
                    ClusterItem.cluster_id == cluster_id,
                    FeedItem.processing_status.in_(
                        (ProcessingStatus.NORMALIZED, ProcessingStatus.PROCESSED)
                    ),
                )
                .order_by(
                    ClusterItem.is_primary.desc(),
                    FeedItem.published_at.desc().nulls_last(),
                )
            )
            member_rows = self.db.execute(cluster_members_query).all()
            items_count = len(member_rows)
            for m_item, m_src in member_rows:
                m_summary = m_item.summary or (
                    (m_item.body_text[:250].rstrip() + "...")
                    if len(m_item.body_text or "") > 250
                    else (m_item.body_text or "")
                )
                related_items.append(
                    FeedItemListItem(
                        id=m_item.id,
                        title=m_item.title or "Без названия",
                        summary=m_summary,
                        published_at=m_item.published_at,
                        source=SourceBrief(
                            id=m_src.id,
                            name=m_src.name,
                            type=m_src.type,
                        ),
                        original_url=m_item.original_url,
                        is_cluster=False,
                        cluster_id=cluster_id,
                        items_count=1,
                    )
                )

        return FeedItemDetail(
            id=feed_item.id,
            title=title,
            summary=summary,
            body_text=feed_item.body_text,
            author=feed_item.author,
            published_at=feed_item.published_at,
            original_url=feed_item.original_url,
            source=SourceBrief(
                id=source.id,
                name=source.name,
                type=source.type,
            ),
            tags=list(dict.fromkeys(tags)),
            extracted_entities=feed_item.extracted_entities,
            is_cluster=is_cluster,
            cluster_id=cluster_id,
            items_count=items_count,
            related_items=related_items,
        )
