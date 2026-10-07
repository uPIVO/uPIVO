from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Identity, Integer, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class RedditSyncState(Base):
    __tablename__ = "reddit_sync_state"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    source_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("sources.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    cursor_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    initial_floor_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    overlap_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("3600")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )
