"""FastAPI router for user news feed and detailed publication view."""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.enums import SourceType
from app.schemas.feed import FeedItemDetail, PaginatedFeedResponse
from app.services.feed import FeedService

router = APIRouter(prefix="/feed", tags=["News Feed"])
logger = logging.getLogger(__name__)


@router.get(
    "",
    response_model=PaginatedFeedResponse,
    summary="Получение персональной ленты новостей пользователя",
)
def get_user_feed(
    user_id: Annotated[int, Query(description="Идентификатор пользователя", ge=1)],
    source_type: Annotated[
        SourceType | None,
        Query(description="Фильтр по типу источника (habr, reddit, telegram, youtube и др.)"),
    ] = None,
    topic: Annotated[
        str | None,
        Query(description="Фильтр по тематике/тегам (поиск по ключевым словам и тегам)"),
    ] = None,
    limit: Annotated[
        int,
        Query(description="Количество записей на страницу", ge=1, le=100),
    ] = 20,
    offset: Annotated[
        int,
        Query(description="Смещение пагинации", ge=0),
    ] = 0,
    db: Session = Depends(get_db),
):
    """
    Возвращает персонализированную новостную ленту пользователя:
    - Посты из всех подключенных пользователем источников
    - Заголовок поста в верхнем регистре (CAPS)
    - Краткое изложение (AI Summary или превью)
    - Время и дата публикации
    - Источник публикации
    - Возможность фильтрации по источнику (`source_type`) и теме (`topic`)
    """
    feed_service = FeedService(db)
    return feed_service.get_user_feed(
        user_id=user_id,
        source_type=source_type,
        topic=topic,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/{item_id}",
    response_model=FeedItemDetail,
    summary="Получение развернутого полного поста",
)
def get_feed_item_detail(
    item_id: int,
    db: Session = Depends(get_db),
):
    """
    Возвращает полный развернутый пост по его ID:
    - Заголовок и полный текст (`body_text`)
    - Саммари от ИИ
    - Ссылка на оригинальный пост
    - Автор, дата публикации, теги и сущности
    """
    feed_service = FeedService(db)
    item = feed_service.get_feed_item_detail(item_id)
    if not item:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Публикация с ID {item_id} не найдена",
        )
    return item

