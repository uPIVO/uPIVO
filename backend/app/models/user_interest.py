from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Text,
)
from sqlalchemy import text as sa_text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class UserInterest(Base):
    """Один свободный текстовый интерес пользователя.

    Список интересов общий для всех включённых источников; он не связан с
    folders, topics или sources. Порядок чтения — по `id`, и именно он входит
    в отпечаток входных данных персональной оценки.
    """

    __tablename__ = "user_interests"
    __table_args__ = (
        CheckConstraint("length(btrim(text)) > 0", name="ck_user_interests_text_not_blank"),
        Index("idx_user_interests_user_id", "user_id"),
        # Уникальность без учёта регистра и крайних пробелов проверяет БД.
        Index(
            "uq_user_interests_user_text_lower",
            "user_id",
            sa_text("lower(btrim(text))"),
            unique=True,
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", name="fk_user_interests_user_id_users", ondelete="CASCADE"),
        nullable=False,
    )
    text: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), nullable=False, server_default=sa_text("CURRENT_TIMESTAMP")
    )
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
