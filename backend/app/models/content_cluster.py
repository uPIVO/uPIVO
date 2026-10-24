from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
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


class ContentCluster(Base):
    """One story. Member publications stay in feed_items."""

    __tablename__ = "content_clusters"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    title: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text)
    importance_score: Mapped[float | None] = mapped_column(DOUBLE_PRECISION)
    ai_metadata: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )


class ClusterItem(Base):
    """Which feed item belongs to which story. One row is primary."""

    __tablename__ = "cluster_items"
    __table_args__ = (
        UniqueConstraint("cluster_id", "feed_item_id", name="uq_cluster_items_cluster_feed"),
        Index("idx_cluster_items_feed_item_id", "feed_item_id"),
        Index("uq_cluster_items_feed_item_id", "feed_item_id", unique=True),
        Index(
            "uq_cluster_items_primary_per_cluster",
            "cluster_id",
            unique=True,
            postgresql_where=text("is_primary = true"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    cluster_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("content_clusters.id"), nullable=False
    )
    feed_item_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("feed_items.id"), nullable=False
    )
    is_primary: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
