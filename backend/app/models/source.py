from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Identity, Index, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import SourceType, source_type_enum


class Source(Base):
    __tablename__ = "sources"
    __table_args__ = (
        Index("idx_sources_type_external_id", "type", "external_id"),
        Index(
            "uq_sources_telegram_external_id",
            "type",
            "external_id",
            unique=True,
            postgresql_where=text("type = 'telegram'"),
        ),
        Index(
            "uq_sources_reddit_external_id",
            "external_id",
            unique=True,
            postgresql_where=text("type = 'reddit'"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    type: Mapped[SourceType] = mapped_column(source_type_enum, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    canonical_url: Mapped[str] = mapped_column(Text, nullable=False)
    external_id: Mapped[str | None] = mapped_column(String(512))
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )
