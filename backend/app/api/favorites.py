"""FastAPI router for user favorites API."""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.auth import get_current_user_id
from app.db.session import get_db
from app.models.enums import FavoriteTargetType
from app.schemas.favorites import (
    FavoriteCreateRequest,
    FavoriteItemResponse,
    FavoriteUpdateRequest,
    PaginatedFavoritesResponse,
)
from app.services.favorites import (
    FavoriteItemNotFoundError,
    FavoritesService,
    TargetContentUnavailableError,
)
from app.services.users import UnknownUserError

router = APIRouter(prefix="/favorites", tags=["Favorites"])
logger = logging.getLogger(__name__)


@router.post(
    "",
    response_model=FavoriteItemResponse,
    summary="Добавить публикацию или инфоповод в избранное",
    description=(
        "Сохраняет FeedItem или ContentCluster в личное избранное пользователя. "
        "Проверяет доступность материала по логике персональной ленты Feed. "
        "Повторное сохранение идемпотентно возвращает существующую запись (200 OK) "
        "без перезаписи существующей заметки."
    ),
)
def add_to_favorites(
    payload: FavoriteCreateRequest,
    user_id: Annotated[int, Depends(get_current_user_id)],
    db: Annotated[Session, Depends(get_db)],
    response: Response,
) -> FavoriteItemResponse:
    service = FavoritesService(db)
    try:
        fav_item, is_new = service.add_favorite(user_id=user_id, payload=payload)
        response.status_code = status.HTTP_201_CREATED if is_new else status.HTTP_200_OK
        return fav_item
    except UnknownUserError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown user") from exc
    except TargetContentUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Публикация или инфоповод не найдены или недоступны",
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    except IntegrityError as exc:
        logger.warning(
            "IntegrityError in add_to_favorites for user_id=%s, attempting recovery: %s",
            user_id,
            exc,
        )
        db.rollback()
        fav_item = service._find_favorite(user_id, payload.target_type, payload.target_id)
        if fav_item:
            response.status_code = status.HTTP_200_OK
            return service._build_favorite_response(fav_item)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Конфликт сохранения в избранное",
        ) from exc


@router.get(
    "",
    response_model=PaginatedFavoritesResponse,
    summary="Получить список избранного текущего пользователя",
    description=(
        "Возвращает пагинированный список сохраненных материалов "
        "в порядке добавления (свежие первыми)."
    ),
)
def get_user_favorites(
    user_id: Annotated[int, Depends(get_current_user_id)],
    db: Annotated[Session, Depends(get_db)],
    limit: Annotated[int, Query(ge=1, le=100, description="Количество записей на страницу")] = 20,
    offset: Annotated[int, Query(ge=0, description="Смещение")] = 0,
) -> PaginatedFavoritesResponse:
    service = FavoritesService(db)
    try:
        return service.get_user_favorites(user_id=user_id, limit=limit, offset=offset)
    except UnknownUserError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown user") from exc


@router.patch(
    "/{favorite_id}",
    response_model=FavoriteItemResponse,
    summary="Обновить заметку к материалу в избранном",
    description="Обновляет только текст заметки note. Передача null удаляет заметку.",
)
def update_favorite_note(
    favorite_id: int,
    payload: FavoriteUpdateRequest,
    user_id: Annotated[int, Depends(get_current_user_id)],
    db: Annotated[Session, Depends(get_db)],
) -> FavoriteItemResponse:
    service = FavoritesService(db)
    try:
        return service.update_favorite_note(
            user_id=user_id,
            favorite_id=favorite_id,
            payload=payload,
        )
    except UnknownUserError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown user") from exc
    except FavoriteItemNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Запись избранного с ID {favorite_id} не найдена",
        ) from exc


@router.delete(
    "/{favorite_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Удалить запись из избранного",
    description="Удаляет запись из личного избранного. Исходный FeedItem / Cluster не удаляется.",
)
def delete_favorite(
    favorite_id: int,
    user_id: Annotated[int, Depends(get_current_user_id)],
    db: Annotated[Session, Depends(get_db)],
) -> None:
    service = FavoritesService(db)
    try:
        service.delete_favorite(user_id=user_id, favorite_id=favorite_id)
    except UnknownUserError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown user") from exc


@router.delete(
    "",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Удалить из избранного по target_type и target_id",
    description="Альтернативное удаление по типу сущности (feed_item/cluster) и её идентификатору.",
)
def delete_favorite_by_target(
    target_type: Annotated[FavoriteTargetType, Query(description="Тип: feed_item или cluster")],
    target_id: Annotated[int, Query(ge=1, description="ID публикации или кластера")],
    user_id: Annotated[int, Depends(get_current_user_id)],
    db: Annotated[Session, Depends(get_db)],
) -> None:
    service = FavoritesService(db)
    try:
        service.delete_favorite_by_target(
            user_id=user_id,
            target_type=target_type,
            target_id=target_id,
        )
    except UnknownUserError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown user") from exc
