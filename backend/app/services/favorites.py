"""Business logic and database operations for user favorites."""

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.content_cluster import ClusterItem, ContentCluster
from app.models.enums import FavoriteTargetType, ProcessingStatus
from app.models.favorite import Favorite
from app.models.feed_item import FeedItem
from app.models.source import Source
from app.models.user_source import UserSource
from app.schemas.favorites import (
    FavoriteCreateRequest,
    FavoriteItemResponse,
    FavoriteUpdateRequest,
    PaginatedFavoritesResponse,
)
from app.schemas.feed import FeedItemListItem, SourceBrief
from app.services.users import require_user

logger = logging.getLogger(__name__)


class FavoriteItemNotFoundError(LookupError):
    """The favorite record does not exist for the specified user."""


class TargetContentUnavailableError(LookupError):
    """The target feed item or cluster does not exist or is not visible to the user."""


class FavoritesService:
    def __init__(self, db: Session):
        self.db = db

    def check_feed_item_available(self, user_id: int, feed_item_id: int) -> FeedItem:
        """
        Check that the FeedItem exists, is normalized/processed,
        and its source is currently enabled in user_sources for this user.
        """
        query = (
            select(FeedItem)
            .join(
                UserSource,
                (UserSource.source_id == FeedItem.source_id)
                & (UserSource.user_id == user_id)
                & (UserSource.enabled.is_(True)),
            )
            .where(
                FeedItem.id == feed_item_id,
                FeedItem.processing_status.in_(
                    (ProcessingStatus.NORMALIZED, ProcessingStatus.PROCESSED)
                ),
            )
        )
        item = self.db.execute(query).scalar_one_or_none()
        if not item:
            raise TargetContentUnavailableError(
                f"FeedItem {feed_item_id} is unavailable or does not exist"
            )
        return item

    def check_cluster_available(self, user_id: int, cluster_id: int) -> ContentCluster:
        """
        Check that the ContentCluster exists and contains at least one
        feed item from an enabled source of this user with normalized/processed status.
        """
        query = (
            select(ContentCluster)
            .join(ClusterItem, ClusterItem.cluster_id == ContentCluster.id)
            .join(FeedItem, FeedItem.id == ClusterItem.feed_item_id)
            .join(
                UserSource,
                (UserSource.source_id == FeedItem.source_id)
                & (UserSource.user_id == user_id)
                & (UserSource.enabled.is_(True)),
            )
            .where(
                ContentCluster.id == cluster_id,
                FeedItem.processing_status.in_(
                    (ProcessingStatus.NORMALIZED, ProcessingStatus.PROCESSED)
                ),
            )
        )
        cluster = self.db.execute(query).first()
        if not cluster:
            raise TargetContentUnavailableError(
                f"ContentCluster {cluster_id} is unavailable or does not exist"
            )
        return cluster[0]

    def add_favorite(
        self, user_id: int, payload: FavoriteCreateRequest
    ) -> tuple[FavoriteItemResponse, bool]:
        """
        Save an item or cluster to favorites:
        - Checks content availability per feed logic.
        - If already saved, returns existing favorite with 200 without overwriting note.
        - If new, creates favorite and returns 201.
        Returns: (FavoriteItemResponse, is_new: bool)
        """
        require_user(self.db, user_id)

        target_type = payload.target_type
        target_id = payload.target_id
        assert target_id is not None

        # Check availability on creation
        if target_type == FavoriteTargetType.FEED_ITEM:
            self.check_feed_item_available(user_id, target_id)
            existing = self.db.execute(
                select(Favorite).where(
                    Favorite.user_id == user_id,
                    Favorite.target_type == FavoriteTargetType.FEED_ITEM,
                    Favorite.feed_item_id == target_id,
                )
            ).scalar_one_or_none()
        else:
            self.check_cluster_available(user_id, target_id)
            existing = self.db.execute(
                select(Favorite).where(
                    Favorite.user_id == user_id,
                    Favorite.target_type == FavoriteTargetType.CLUSTER,
                    Favorite.cluster_id == target_id,
                )
            ).scalar_one_or_none()

        if existing:
            # Idempotent: return existing without modifying note
            return self._build_favorite_response(existing), False

        # Create new favorite
        fav = Favorite(
            user_id=user_id,
            target_type=target_type,
            feed_item_id=target_id if target_type == FavoriteTargetType.FEED_ITEM else None,
            cluster_id=target_id if target_type == FavoriteTargetType.CLUSTER else None,
            note=payload.note,
        )
        self.db.add(fav)
        self.db.commit()
        self.db.refresh(fav)
        return self._build_favorite_response(fav), True

    def get_user_favorites(
        self,
        user_id: int,
        limit: int = 20,
        offset: int = 0,
    ) -> PaginatedFavoritesResponse:
        """List user's favorites in reverse chronological order (created_at DESC)."""
        require_user(self.db, user_id)

        base_query = select(Favorite).where(Favorite.user_id == user_id)
        # Total count
        total = len(self.db.execute(base_query).scalars().all())

        ordered_query = (
            base_query.order_by(Favorite.created_at.desc(), Favorite.id.desc())
            .offset(offset)
            .limit(limit)
        )
        rows = self.db.execute(ordered_query).scalars().all()

        items = [self._build_favorite_response(fav) for fav in rows]
        return PaginatedFavoritesResponse(
            items=items,
            total=total,
            limit=limit,
            offset=offset,
        )

    def update_favorite_note(
        self,
        user_id: int,
        favorite_id: int,
        payload: FavoriteUpdateRequest,
    ) -> FavoriteItemResponse:
        """Update note of existing favorite record."""
        require_user(self.db, user_id)

        fav = self.db.execute(
            select(Favorite).where(
                Favorite.id == favorite_id,
                Favorite.user_id == user_id,
            )
        ).scalar_one_or_none()

        if not fav:
            raise FavoriteItemNotFoundError(f"Favorite {favorite_id} not found for user {user_id}")

        fav.note = payload.note
        self.db.commit()
        self.db.refresh(fav)
        return self._build_favorite_response(fav)

    def delete_favorite(self, user_id: int, favorite_id: int) -> None:
        """
        Delete favorite by favorite_id.
        Idempotent: if favorite is already missing or belongs to another user,
        does nothing (or returns cleanly) after confirming user existence.
        """
        require_user(self.db, user_id)

        fav = self.db.execute(
            select(Favorite).where(
                Favorite.id == favorite_id,
                Favorite.user_id == user_id,
            )
        ).scalar_one_or_none()

        if fav:
            self.db.delete(fav)
            self.db.commit()

    def delete_favorite_by_target(
        self,
        user_id: int,
        target_type: FavoriteTargetType,
        target_id: int,
    ) -> None:
        """Delete favorite by target_type and target_id (convenience helper)."""
        require_user(self.db, user_id)

        if target_type == FavoriteTargetType.FEED_ITEM:
            fav = self.db.execute(
                select(Favorite).where(
                    Favorite.user_id == user_id,
                    Favorite.target_type == FavoriteTargetType.FEED_ITEM,
                    Favorite.feed_item_id == target_id,
                )
            ).scalar_one_or_none()
        else:
            fav = self.db.execute(
                select(Favorite).where(
                    Favorite.user_id == user_id,
                    Favorite.target_type == FavoriteTargetType.CLUSTER,
                    Favorite.cluster_id == target_id,
                )
            ).scalar_one_or_none()

        if fav:
            self.db.delete(fav)
            self.db.commit()

    def _build_favorite_response(self, fav: Favorite) -> FavoriteItemResponse:
        """Construct a response with a card matching FeedItemListItem representation."""
        if fav.target_type == FavoriteTargetType.CLUSTER and fav.cluster_id:
            cluster = fav.cluster or self.db.get(ContentCluster, fav.cluster_id)
            # Find primary item and related sources
            query = (
                select(FeedItem, Source, ClusterItem.is_primary)
                .join(ClusterItem, ClusterItem.feed_item_id == FeedItem.id)
                .join(Source, FeedItem.source_id == Source.id)
                .where(ClusterItem.cluster_id == fav.cluster_id)
                .order_by(ClusterItem.is_primary.desc(), FeedItem.published_at.desc().nulls_last())
            )
            rows = self.db.execute(query).all()

            if rows:
                primary_item, primary_source, _ = rows[0]
            else:
                primary_item = None
                primary_source = None

            related_sources = []
            seen_src = set()
            for _, src, _ in rows:
                if src.id not in seen_src:
                    seen_src.add(src.id)
                    related_sources.append(SourceBrief(id=src.id, name=src.name, type=src.type))

            title = (
                cluster.title
                if cluster and cluster.title
                else (primary_item.title if primary_item else "Без названия")
            ) or "Без названия"
            summary = (
                cluster.summary
                if cluster and cluster.summary
                else (primary_item.summary if primary_item else None)
            ) or ""

            card = FeedItemListItem(
                id=primary_item.id if primary_item else 0,
                title=title,
                summary=summary,
                published_at=primary_item.published_at if primary_item else None,
                source=SourceBrief(
                    id=primary_source.id if primary_source else 0,
                    name=primary_source.name if primary_source else "Unknown",
                    type=primary_source.type if primary_source else "website",
                ),
                original_url=primary_item.original_url if primary_item else "",
                is_cluster=True,
                cluster_id=fav.cluster_id,
                items_count=len(rows),
                related_sources=related_sources,
            )

            return FavoriteItemResponse(
                id=fav.id,
                user_id=fav.user_id,
                target_type=FavoriteTargetType.CLUSTER,
                target_id=fav.cluster_id,
                feed_item_id=None,
                cluster_id=fav.cluster_id,
                note=fav.note,
                created_at=fav.created_at,
                card=card,
            )

        # Single FeedItem favorite
        feed_item = fav.feed_item or self.db.get(FeedItem, fav.feed_item_id)
        source = (
            self.db.get(Source, feed_item.source_id) if feed_item and feed_item.source_id else None
        )

        title = (feed_item.title if feed_item else "Без названия") or "Без названия"
        summary = (
            feed_item.summary
            if feed_item and feed_item.summary
            else (
                (feed_item.body_text[:250].rstrip() + "...")
                if feed_item and len(feed_item.body_text or "") > 250
                else (feed_item.body_text if feed_item else "")
            )
        )

        source_brief = SourceBrief(
            id=source.id if source else 0,
            name=source.name if source else "Unknown",
            type=source.type if source else "website",
        )

        card = FeedItemListItem(
            id=feed_item.id if feed_item else 0,
            title=title,
            summary=summary,
            published_at=feed_item.published_at if feed_item else None,
            source=source_brief,
            original_url=feed_item.original_url if feed_item else "",
            is_cluster=False,
            cluster_id=None,
            items_count=1,
            related_sources=[source_brief] if source else [],
        )

        return FavoriteItemResponse(
            id=fav.id,
            user_id=fav.user_id,
            target_type=FavoriteTargetType.FEED_ITEM,
            target_id=fav.feed_item_id or 0,
            feed_item_id=fav.feed_item_id,
            cluster_id=None,
            note=fav.note,
            created_at=fav.created_at,
            card=card,
        )
