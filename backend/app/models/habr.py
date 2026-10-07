from datetime import datetime
from sqlalchemy import DateTime, Integer, String, Text, JSON, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base  # Путь к базовому DeclarativeBase вашего проекта


class HabrPost(Base):
    __tablename__ = "habr_posts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    habr_id: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    author: Mapped[str] = mapped_column(String(255), nullable=False)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True, nullable=False)
    
    # Хабы и теги храним в JSON-массиве
    hubs: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    tags: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    
    score: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    reading_time: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    original_url: Mapped[str] = mapped_column(String(512), nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )