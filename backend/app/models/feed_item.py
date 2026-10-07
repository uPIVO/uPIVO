from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import DOUBLE_PRECISION, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import ProcessingStatus, processing_status_enum


class FeedItem(Base):
    """Общая публикация; коннекторы заполняют исходные поля без запуска ИИ."""

    __tablename__ = "feed_items"
    __table_args__ = (
        UniqueConstraint("source_id", "external_id", name="uq_feed_items_source_external"),
        Index("idx_feed_items_content_hash", "content_hash"),
        Index("idx_feed_items_published_at", "published_at"),
        Index("idx_feed_items_source_id", "source_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    source_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("sources.id"), nullable=False)
    external_id: Mapped[str] = mapped_column(String(512), nullable=False)
    title: Mapped[str | None] = mapped_column(Text)
    body_text: Mapped[str] = mapped_column(Text, nullable=False)
    original_url: Mapped[str] = mapped_column(Text, nullable=False)
    author: Mapped[str | None] = mapped_column(String(255))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    collected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )
    raw_payload: Mapped[dict | None] = mapped_column(JSONB)
    extracted_entities: Mapped[dict | None] = mapped_column(JSONB)
    content_hash: Mapped[str | None] = mapped_column(String(64))
    summary: Mapped[str | None] = mapped_column(Text)
    importance_score: Mapped[float | None] = mapped_column(DOUBLE_PRECISION)
    processing_status: Mapped[ProcessingStatus] = mapped_column(
        processing_status_enum,
        nullable=False,
        server_default=text("'pending'"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
