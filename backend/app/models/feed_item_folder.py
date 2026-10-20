from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Identity, Index, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class FeedItemFolder(Base):
    """Links a shared feed item to a user's topic. The feed item stays one row."""

    __tablename__ = "feed_item_folders"
    __table_args__ = (
        UniqueConstraint("folder_id", "feed_item_id", name="uq_feed_item_folders_folder_feed"),
        Index("idx_feed_item_folders_feed_item_id", "feed_item_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    folder_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("folders.id"), nullable=False)
    feed_item_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("feed_items.id"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )
