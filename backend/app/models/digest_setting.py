from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class UserDigestSetting(Base):
    """Пользовательские настройки генерации дайджестов."""

    __tablename__ = "user_digest_settings"
    __table_args__ = (
        CheckConstraint(
            "period_hours >= 1 AND period_hours <= 168",
            name="chk_user_digest_settings_period_hours",
        ),
        CheckConstraint(
            "max_items >= 1 AND max_items <= 100",
            name="chk_user_digest_settings_max_items",
        ),
    )

    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    period_hours: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text("24"),
    )
    max_items: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text("15"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=False),
        nullable=True,
        onupdate=datetime.utcnow,
    )

    user = relationship("User", backref="digest_settings")
