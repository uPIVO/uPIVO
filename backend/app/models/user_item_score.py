from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import DOUBLE_PRECISION, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class UserItemScore(Base):
    """Персональная оценка пользователя для FeedItem или ContentCluster."""

    __tablename__ = "user_item_scores"
    __table_args__ = (
        CheckConstraint(
            "relevance_score IS NULL OR (relevance_score >= 0.0 AND relevance_score <= 1.0)",
            name="ck_user_item_scores_relevance_range",
        ),
        CheckConstraint(
            "(feed_item_id IS NOT NULL AND cluster_id IS NULL) OR "
            "(feed_item_id IS NULL AND cluster_id IS NOT NULL)",
            name="ck_user_item_scores_exactly_one_target",
        ),
        UniqueConstraint("user_id", "feed_item_id", name="uq_user_item_scores_user_feed"),
        UniqueConstraint("user_id", "cluster_id", name="uq_user_item_scores_user_cluster"),
        Index("idx_user_item_scores_feed_item_id", "feed_item_id"),
        Index("idx_user_item_scores_cluster_id", "cluster_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", name="fk_user_item_scores_user_id_users", ondelete="CASCADE"),
        nullable=False,
    )
    feed_item_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey(
            "feed_items.id", name="fk_user_item_scores_feed_item_id_feed_items", ondelete="CASCADE"
        ),
        nullable=True,
    )
    cluster_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey(
            "content_clusters.id",
            name="fk_user_item_scores_cluster_id_content_clusters",
            ondelete="CASCADE",
        ),
        nullable=True,
    )
    relevance_score: Mapped[float | None] = mapped_column(DOUBLE_PRECISION)
    rank_score: Mapped[float | None] = mapped_column(DOUBLE_PRECISION)
    reason: Mapped[str | None] = mapped_column(Text)
    calculated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )
    analysis_metadata: Mapped[dict | None] = mapped_column(JSONB)
