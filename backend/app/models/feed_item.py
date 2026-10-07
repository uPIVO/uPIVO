from datetime import datetime
from sqlalchemy import DateTime, Integer, String, Text, JSON, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base  # Базовый класс моделей вашего проекта


class FeedItem(Base):
    __tablename__ = "feed_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(64), index=True, nullable=False)  # например, "habr"
    external_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False)  # ID на Хабре
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    author: Mapped[str] = mapped_column(String(255), nullable=True)
    url: Mapped[str] = mapped_column(String(512), nullable=False)
    published_at: Mapped[datetime] =S mapped_column(DateTime(timezone=True), index=True, nullable=True)

    # Теги, хабы, рейтинг и прочее сохраняются в JSON
    raw_data: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )