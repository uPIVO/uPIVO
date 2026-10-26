from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.enums import DigestType, digest_type_enum


class Digest(Base):
    """Generated summary digest for a user."""

    __tablename__ = "digests"
    __table_args__ = (
        Index("idx_digests_user_id", "user_id"),
        Index("idx_digests_created_at", "created_at"),
        Index("idx_digests_user_topic", "user_id", "topic"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), nullable=False)
    type: Mapped[DigestType] = mapped_column(
        digest_type_enum, nullable=False, server_default=text("'general'")
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    topic: Mapped[str | None] = mapped_column(String(255))
    summary_text: Mapped[str] = mapped_column(Text, nullable=False)
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    items_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    items_snapshot: Mapped[list[dict]] = mapped_column(JSON, nullable=False)
    is_favorite: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )

    items: Mapped[list["DigestItem"]] = relationship(
        "DigestItem", back_populates="digest", cascade="all, delete-orphan"
    )


class DigestItem(Base):
    """Link between a digest and feed items included in it."""

    __tablename__ = "digest_items"
    __table_args__ = (
        Index("idx_digest_items_digest_id", "digest_id"),
        Index("idx_digest_items_feed_item_id", "feed_item_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    digest_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("digests.id", ondelete="CASCADE"), nullable=False
    )
    feed_item_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("feed_items.id", ondelete="CASCADE"), nullable=False
    )
    included_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))

    digest: Mapped["Digest"] = relationship("Digest", back_populates="items")
