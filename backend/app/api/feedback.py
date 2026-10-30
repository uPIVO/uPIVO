"""HTTP API обратной связи по публикациям и инфоповодам.

Нужен подписанный Bearer-токен владельца указанного `users.id`.
Оценивается карточка, которую может вернуть лента: одиночная публикация
(`feed_item`, не входящая в инфоповод) или целый инфоповод (`cluster`).
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Response, status
from sqlalchemy.orm import Session

from app.api.security import require_same_user
from app.db.session import get_db
from app.schemas.feedback import FeedbackKind, FeedbackPutRequest, FeedbackResponse
from app.services.feedback import (
    UnavailableFeedbackTargetError,
    UnknownFeedbackError,
    delete_feedback,
    get_feedback,
    list_feedback,
    set_feedback,
)
from app.services.users import UnknownUserError

_AUTH = "Нужен Bearer-токен этого пользователя."
_MAX_UNIT_IDS = 100

router = APIRouter(
    prefix="/users/{user_id}/feedback", tags=["feedback"], dependencies=[Depends(require_same_user)]
)


def _raise_http(exc: Exception) -> None:
    if isinstance(exc, UnknownUserError):
        raise HTTPException(status_code=404, detail="Пользователь не найден.") from exc
    if isinstance(exc, UnknownFeedbackError):
        raise HTTPException(status_code=404, detail="Реакция не найдена.") from exc
    if isinstance(exc, UnavailableFeedbackTargetError):
        raise HTTPException(status_code=404, detail="Материал недоступен.") from exc
    raise exc


@router.get(
    "",
    response_model=list[FeedbackResponse],
    summary="Реакции пользователя",
    description=(
        "Текущие реакции в порядке `created_at DESC, id DESC`; пустой список — "
        "штатное состояние. С `kind` и повторяемым `unit_id` (1–100 значений) "
        f"возвращаются реакции на конкретные видимые карточки без пагинации. {_AUTH}"
    ),
)
def get_feedback_list(
    user_id: Annotated[int, Path(ge=1)],
    session: Annotated[Session, Depends(get_db)],
    kind: Annotated[
        FeedbackKind | None,
        Query(description="Отбор по типу единицы: feed_item или cluster"),
    ] = None,
    unit_id: Annotated[
        list[int] | None,
        Query(
            description=(
                "ID единиц нужного типа для подгрузки оценок видимых карточек; "
                "допустим только вместе с kind"
            )
        ),
    ] = None,
    limit: Annotated[int, Query(description="Размер страницы", ge=1, le=100)] = 20,
    offset: Annotated[int, Query(description="Смещение пагинации", ge=0)] = 0,
) -> list[FeedbackResponse]:
    if unit_id is not None:
        if kind is None:
            raise HTTPException(
                status_code=422, detail="Параметр unit_id допустим только вместе с kind."
            )
        if not 1 <= len(unit_id) <= _MAX_UNIT_IDS:
            raise HTTPException(
                status_code=422, detail=f"unit_id принимает от 1 до {_MAX_UNIT_IDS} значений."
            )
        if any(value < 1 for value in unit_id):
            raise HTTPException(status_code=422, detail="unit_id должен быть положительным.")
    try:
        return list_feedback(
            session, user_id, kind=kind, unit_ids=unit_id, limit=limit, offset=offset
        )
    except UnknownUserError as exc:
        _raise_http(exc)
        raise


@router.get(
    "/{kind}/{unit_id}",
    response_model=FeedbackResponse,
    summary="Текущая реакция на единицу",
    description=(
        "Отсутствие реакции — `404`; это не означает, что публикации нет. "
        f"Чтение своей реакции работает и после отключения источника. {_AUTH}"
    ),
)
def get_feedback_item(
    user_id: Annotated[int, Path(ge=1)],
    kind: FeedbackKind,
    unit_id: Annotated[int, Path(ge=1)],
    session: Annotated[Session, Depends(get_db)],
) -> FeedbackResponse:
    try:
        return get_feedback(session, user_id, kind, unit_id)
    except (UnknownUserError, UnknownFeedbackError) as exc:
        _raise_http(exc)
        raise


@router.put(
    "/{kind}/{unit_id}",
    response_model=FeedbackResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        status.HTTP_200_OK: {
            "description": "Реакция уже была: заменена или совпала без изменения updated_at.",
            "model": FeedbackResponse,
        }
    },
    summary="Поставить или заменить реакцию",
    description=(
        "Новая реакция — `201`, имеющаяся — `200`. Повтор той же реакции не меняет "
        "`updated_at`; другая реакция перезаписывает ту же строку и обновляет "
        f"`updated_at`. Нет третьей нейтральной реакции: «без оценки» — это DELETE. {_AUTH}"
    ),
)
def put_feedback_item(
    user_id: Annotated[int, Path(ge=1)],
    kind: FeedbackKind,
    unit_id: Annotated[int, Path(ge=1)],
    payload: FeedbackPutRequest,
    response: Response,
    session: Annotated[Session, Depends(get_db)],
) -> FeedbackResponse:
    try:
        feedback, created = set_feedback(session, user_id, kind, unit_id, payload.reaction)
    except (UnknownUserError, UnavailableFeedbackTargetError) as exc:
        _raise_http(exc)
        raise
    response.status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
    return feedback


@router.delete(
    "/{kind}/{unit_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Снять реакцию",
    description=(
        "Физически удаляет только строку `feedback` своего пользователя. Повторное "
        f"удаление — тоже `204`. {_AUTH}"
    ),
)
def delete_feedback_item(
    user_id: Annotated[int, Path(ge=1)],
    kind: FeedbackKind,
    unit_id: Annotated[int, Path(ge=1)],
    session: Annotated[Session, Depends(get_db)],
) -> Response:
    try:
        delete_feedback(session, user_id, kind, unit_id)
    except UnknownUserError as exc:
        _raise_http(exc)
        raise
    return Response(status_code=status.HTTP_204_NO_CONTENT)
