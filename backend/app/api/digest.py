"""FastAPI router for user news digests."""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.enums import DigestType
from app.schemas.digest import (
    DigestCreateRequest,
    DigestDetailResponse,
    DigestListItem,
    DigestToggleFavoriteResponse,
)
from app.services.digest import DigestService

router = APIRouter(prefix="/digests", tags=["Digests"])
logger = logging.getLogger(__name__)


@router.post(
    "/generate",
    response_model=DigestDetailResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Сгенерировать дайджест по запросу пользователя",
)
def generate_digest(
    request: DigestCreateRequest,
    db: Annotated[Session, Depends(get_db)],
):
    """
    Генерирует дайджест за выбранный период (по умолчанию 24 часа):
    - Тип: общий (`general`) или тематический (`topic`).
    - Исключает дубли из прошлых дайджестов пользователя,
      если публикация с тех пор не обновлялась.
    - Ранжирует материалы по важности (`importance_score`).
    - Формирует кликабельную плашку с названием за дату/время и сохраняет результат в БД.
    - Автоматически подчищает старые дайджесты старше 3 дней (кроме избранных).
    """
    if request.type == DigestType.TOPIC and not (request.topic and request.topic.strip()):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Для тематического дайджеста необходимо указать тему (topic)",
        )

    service = DigestService(db)
    return service.generate_digest(request)


@router.get(
    "",
    response_model=list[DigestListItem],
    summary="Получить список плашек дайджестов пользователя",
)
def list_digests(
    user_id: Annotated[int, Query(description="ID пользователя", ge=1)],
    db: Annotated[Session, Depends(get_db)],
    type: Annotated[DigestType | None, Query(description="Фильтр: general или topic")] = None,
    only_favorites: Annotated[bool, Query(description="Только сохранённые в избранное")] = False,
):
    """
    Возвращает список кликабельных плашек дайджестов для вкладки «Дайджесты»:
    - Название и дата/время сборки (например, 'Дайджест за 18.10.2026 19:30')
    - Краткое превью
    - Количество материалов
    - Флаг избранного
    """
    service = DigestService(db)
    return service.list_user_digests(
        user_id=user_id,
        digest_type=type,
        only_favorites=only_favorites,
    )


@router.get(
    "/{digest_id}",
    response_model=DigestDetailResponse,
    summary="Получить развернутый дайджест при клике на плашку",
)
def get_digest_detail(
    digest_id: int,
    db: Annotated[Session, Depends(get_db)],
):
    """
    Возвращает полный текст дайджеста и все публикации, вошедшие в него:
    - Заголовок и вводная сводка
    - Список публикаций с названиями заглавными буквами, ссылками на оригиналы и источниками
    """
    service = DigestService(db)
    digest = service.get_digest_detail(digest_id)
    if not digest:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Дайджест с ID {digest_id} не найден",
        )
    return digest


@router.patch(
    "/{digest_id}/favorite",
    response_model=DigestToggleFavoriteResponse,
    summary="Добавить дайджест в избранное / убрать из избранного",
)
def toggle_favorite(
    digest_id: int,
    db: Annotated[Session, Depends(get_db)],
):
    """
    Переключает флаг избранного.
    Дайджесты в избранном НЕ удаляются по истечении 3 дней.
    """
    service = DigestService(db)
    is_fav = service.toggle_favorite(digest_id)
    if is_fav is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Дайджест с ID {digest_id} не найден",
        )
    msg = (
        "Дайджест сохранён в избранное и не будет удален через 3 дня"
        if is_fav
        else "Дайджест удалён из избранного"
    )
    return DigestToggleFavoriteResponse(id=digest_id, is_favorite=is_fav, message=msg)
