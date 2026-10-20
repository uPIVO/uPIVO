from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Identity, Index, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Folder(Base):
    """A user's theme over the shared feed. Not a feed copy and not favorites."""

    __tablename__ = "folders"
    __table_args__ = (
        Index("idx_folders_user_id", "user_id"),
        Index("idx_folders_parent_id", "parent_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    parent_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("folders.id"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
