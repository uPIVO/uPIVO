"""Service for generating personalized user news feed and detailed item view."""

import logging

from sqlalchemy import String, cast, func, or_, select
from sqlalchemy.orm import Session

from app.models.enums import ProcessingStatus, SourceType
from app.models.feed_item import FeedItem
from app.models.source import Source
from app.models.user_source import UserSource
from app.schemas.feed import (
    FeedItemDetail,
    FeedItemListItem,
    PaginatedFeedResponse,
    SourceBrief,
)
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
        4. Sort by publication date descending (published_at DESC, collected_at DESC).
        5. Return the original title, summary, publication date, source info, and link.
        """
        require_user(self.db, user_id)
        # Base query joining FeedItem, Source, and UserSource
        base_query = (
            select(FeedItem, Source)
            .join(Source, FeedItem.source_id == Source.id)
            .join(
                UserSource,
                (UserSource.source_id == Source.id)
                & (UserSource.user_id == user_id)
                & (UserSource.enabled.is_(True)),
            )
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

            # Search in title, summary, body_text or JSON tags/hubs in raw_payload
            raw_payload_text = cast(FeedItem.raw_payload, String)
            topic_filter = or_(
                FeedItem.title.ilike(pattern),
                FeedItem.summary.ilike(pattern),
                FeedItem.body_text.ilike(pattern),
                raw_payload_text.ilike(pattern),
            )
            base_query = base_query.where(topic_filter)

        # Count total matching items
        count_query = select(func.count()).select_from(base_query.subquery())
        total = self.db.scalar(count_query) or 0

        # Query ordered paginated items
        items_query = (
            base_query.order_by(
                FeedItem.published_at.desc().nulls_last(),
                FeedItem.collected_at.desc(),
                FeedItem.id.desc(),
            )
            .offset(offset)
            .limit(limit)
        )

        rows = self.db.execute(items_query).all()

        feed_items: list[FeedItemListItem] = []
        for feed_item, source in rows:
            title_text = (feed_item.title or "Без названия").strip()

            # 2. Summary: Use AI-generated summary if present; fallback to truncated body_text
            summary_text = feed_item.summary
            if not summary_text:
                raw_body = (feed_item.body_text or "").strip()
                if len(raw_body) > summary_fallback_length:
                    summary_text = raw_body[:summary_fallback_length].rstrip() + "..."
                else:
                    summary_text = raw_body

            feed_items.append(
                FeedItemListItem(
                    id=feed_item.id,
                    title=title_text,
                    summary=summary_text,
                    published_at=feed_item.published_at,
                    source=SourceBrief(
                        id=source.id,
                        name=source.name,
                        type=source.type,
                    ),
                    original_url=feed_item.original_url,
                )
            )

        return PaginatedFeedResponse(
            items=feed_items,
            total=total,
            limit=limit,
            offset=offset,
        )

    def get_feed_item_detail(self, user_id: int, item_id: int) -> FeedItemDetail | None:
        """Retrieve details only for an item in this user's enabled sources."""
        require_user(self.db, user_id)
        query = (
            select(FeedItem, Source)
            .join(Source, FeedItem.source_id == Source.id)
            .join(
                UserSource,
                (UserSource.source_id == Source.id)
                & (UserSource.user_id == user_id)
                & (UserSource.enabled.is_(True)),
            )
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

        feed_item, source = row

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
        )
