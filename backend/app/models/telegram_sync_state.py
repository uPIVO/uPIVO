from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class TelegramSyncState(Base):
    """Курсор последнего успешно сохранённого сообщения канала Telegram."""

    __tablename__ = "telegram_sync_state"
    __table_args__ = (
        CheckConstraint("last_message_id >= 0", name="ck_telegram_sync_state_last_message_id"),
    )

    source_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("sources.id"), primary_key=True
    )
    last_message_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
