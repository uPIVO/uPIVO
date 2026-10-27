"""HTTP API интересов пользователя.

Авторизация не входит в этот API: в пути передаётся существующий `users.id`.
Сервис проверяет принадлежность интересов пользователю, поэтому чужой
`interest_id` недоступен.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Response
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.schemas.interest import InterestCreateRequest, InterestResponse, InterestUpdateRequest
from app.services.user_interests import (
    DuplicateInterestError,
    InvalidInterestError,
    UnknownInterestError,
    create_interest,
    delete_interest,
    list_interests,
    update_interest,
)
from app.services.users import UnknownUserError

_AUTH = "Авторизация в этот API не входит: user_id — это существующий users.id."

router = APIRouter(prefix="/users/{user_id}/interests", tags=["interests"])


def _raise_http(exc: Exception) -> None:
    if isinstance(exc, UnknownUserError):
        raise HTTPException(status_code=404, detail="Пользователь не найден.") from exc
    if isinstance(exc, UnknownInterestError):
        raise HTTPException(status_code=404, detail="Интерес пользователя не найден.") from exc
    if isinstance(exc, DuplicateInterestError):
        raise HTTPException(
            status_code=409, detail="Такой интерес у пользователя уже существует."
        ) from exc
    if isinstance(exc, InvalidInterestError):
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    raise exc


@router.get(
    "",
    response_model=list[InterestResponse],
    summary="Список интересов пользователя",
    description=(
        "Полный список в устойчивом порядке; пустой список — штатное состояние, "
        f"персональная оценка тогда не выполняется. {_AUTH}"
    ),
)
def get_interests(
    user_id: Annotated[int, Path(ge=1)],
    session: Annotated[Session, Depends(get_db)],
) -> list[InterestResponse]:
    try:
        return list_interests(session, user_id)
    except UnknownUserError as exc:
        _raise_http(exc)
        raise


@router.post(
    "",
    response_model=InterestResponse,
    status_code=201,
    summary="Добавить интерес",
    description=(
        "Один свободный текст без привязки к источнику или папке. Изменение списка "
        f"делает прежние персональные оценки неактуальными. {_AUTH}"
    ),
)
def post_interest(
    user_id: Annotated[int, Path(ge=1)],
    payload: InterestCreateRequest,
    session: Annotated[Session, Depends(get_db)],
) -> InterestResponse:
    try:
        return create_interest(session, user_id, payload.text)
    except (UnknownUserError, DuplicateInterestError, InvalidInterestError) as exc:
        _raise_http(exc)
        raise


@router.patch(
    "/{interest_id}",
    response_model=InterestResponse,
    summary="Заменить текст интереса",
    description=(
        "Полная замена текста. Повтор того же нормализованного текста ничего не "
        f"меняет и не запускает пересчёт. {_AUTH}"
    ),
)
def patch_interest(
    user_id: Annotated[int, Path(ge=1)],
    interest_id: Annotated[int, Path(ge=1)],
    payload: InterestUpdateRequest,
    session: Annotated[Session, Depends(get_db)],
) -> InterestResponse:
    try:
        return update_interest(session, user_id, interest_id, payload.text)
    except (
        UnknownUserError,
        UnknownInterestError,
        DuplicateInterestError,
        InvalidInterestError,
    ) as exc:
        _raise_http(exc)
        raise


@router.delete(
    "/{interest_id}",
    status_code=204,
    summary="Удалить интерес",
    description=(
        "Удаляет одну запись пользователя. Удаление последнего интереса тоже "
        f"делает прежние оценки неактуальными, но платного пересчёта нет. {_AUTH}"
    ),
)
def delete_interest_endpoint(
    user_id: Annotated[int, Path(ge=1)],
    interest_id: Annotated[int, Path(ge=1)],
    session: Annotated[Session, Depends(get_db)],
) -> Response:
    try:
        delete_interest(session, user_id, interest_id)
    except (UnknownUserError, UnknownInterestError) as exc:
        _raise_http(exc)
    return Response(status_code=204)
