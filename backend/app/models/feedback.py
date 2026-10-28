from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import FeedbackType, feedback_type_enum


class Feedback(Base):
    """Текущая реакция пользователя на одну отображаемую единицу контента.

    Строка описывает ровно одну единицу: одиночную публикацию (`feed_item_id`)
    либо инфоповод (`cluster_id`). Числовые ID разных таблиц независимы, поэтому
    тип единицы хранится самой колонкой. `created_at` и `updated_at` описывают
    только текущее состояние: истории реакций нет, `updated_at` заполняется лишь
    при фактической смене реакции.
    """

    __tablename__ = "feedback"
    __table_args__ = (
        CheckConstraint(
            "(feed_item_id IS NOT NULL AND cluster_id IS NULL) OR "
            "(feed_item_id IS NULL AND cluster_id IS NOT NULL)",
            name="ck_feedback_exactly_one_target",
        ),
        UniqueConstraint("user_id", "feed_item_id", name="uq_feedback_user_feed_item"),
        UniqueConstraint("user_id", "cluster_id", name="uq_feedback_user_cluster"),
        Index(
            "idx_feedback_user_created_at",
            "user_id",
            text("created_at DESC"),
            text("id DESC"),
        ),
        # Отдельные индексы ускоряют каскадную очистку по внешним ключам.
        Index("idx_feedback_feed_item_id", "feed_item_id"),
        Index("idx_feedback_cluster_id", "cluster_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", name="fk_feedback_user_id_users", ondelete="CASCADE"),
        nullable=False,
    )
    feed_item_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey(
            "feed_items.id", name="fk_feedback_feed_item_id_feed_items", ondelete="CASCADE"
        ),
        nullable=True,
    )
    cluster_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey(
            "content_clusters.id",
            name="fk_feedback_cluster_id_content_clusters",
            ondelete="CASCADE",
        ),
        nullable=True,
    )
    reaction: Mapped[FeedbackType] = mapped_column(feedback_type_enum, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
