from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.content_cluster import ContentCluster
from app.models.enums import FavoriteTargetType
from app.models.feed_item import FeedItem
from app.models.user import User


class Favorite(Base):
    """Личное избранное пользователя (публикации FeedItem или инфоповоды ContentCluster)."""

    __tablename__ = "favorites"
    __table_args__ = (
        CheckConstraint(
            "(target_type = 'feed_item' AND feed_item_id IS NOT NULL AND cluster_id IS NULL) OR "
            "(target_type = 'cluster' AND cluster_id IS NOT NULL AND feed_item_id IS NULL)",
            name="chk_favorites_target_reference",
        ),
        UniqueConstraint(
            "user_id",
            "target_type",
            "feed_item_id",
            name="uq_favorites_user_feed_item",
        ),
        UniqueConstraint(
            "user_id",
            "target_type",
            "cluster_id",
            name="uq_favorites_user_cluster",
        ),
        Index(
            "idx_favorites_user_id_created_at", "user_id", text("created_at DESC"), text("id DESC")
        ),
        Index("idx_favorites_feed_item_id", "feed_item_id"),
        Index("idx_favorites_cluster_id", "cluster_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    target_type: Mapped[FavoriteTargetType] = mapped_column(
        String(16),
        nullable=False,
        server_default=text("'feed_item'"),
    )
    feed_item_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("feed_items.id", ondelete="CASCADE"), nullable=True
    )
    cluster_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("content_clusters.id", ondelete="CASCADE"), nullable=True
    )
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )

    user: Mapped[User] = relationship("User", lazy="joined")
    feed_item: Mapped[FeedItem | None] = relationship("FeedItem", lazy="joined")
    cluster: Mapped[ContentCluster | None] = relationship("ContentCluster", lazy="joined")
