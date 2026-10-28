"""Register and log in with email and password. These routes stay public."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.session import get_db
from app.schemas.auth import LoginRequest, LoginResponse, RegisteredUser, RegisterRequest
from app.services.auth import (
    DuplicateEmailError,
    InvalidRegistrationError,
    UnknownLoginError,
    WrongPasswordError,
    login_user,
    register_user,
)

router = APIRouter(prefix="/auth", tags=["auth"])


def _raise_http(exc: Exception) -> None:
    if isinstance(exc, DuplicateEmailError):
        raise HTTPException(status_code=409, detail="Email already registered") from exc
    if isinstance(exc, InvalidRegistrationError):
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if isinstance(exc, UnknownLoginError):
        raise HTTPException(status_code=401, detail="Unknown user") from exc
    if isinstance(exc, WrongPasswordError):
        raise HTTPException(status_code=401, detail="Wrong password") from exc
    raise exc


@router.post(
    "/register",
    response_model=RegisteredUser,
    status_code=201,
    summary="Регистрация",
    description=(
        "Создаёт users по email и паролю. В базе хранится только password_hash. "
        "Повторный email — 409."
    ),
)
def post_register(
    payload: RegisterRequest,
    session: Annotated[Session, Depends(get_db)],
) -> RegisteredUser:
    try:
        user = register_user(session, payload.email, payload.password)
    except (DuplicateEmailError, InvalidRegistrationError) as exc:
        _raise_http(exc)
        raise
    return RegisteredUser(id=user.id, email=user.email, created_at=user.created_at)


@router.post(
    "/login",
    response_model=LoginResponse,
    summary="Вход",
    description=(
        "Проверяет email и пароль и возвращает подписанный токен. "
        "Неизвестный email или неверный пароль — 401."
    ),
)
def post_login(
    payload: LoginRequest,
    session: Annotated[Session, Depends(get_db)],
) -> LoginResponse:
    settings = get_settings()
    try:
        result = login_user(
            session,
            payload.email,
            payload.password,
            secret=settings.auth_token_secret.get_secret_value(),
            ttl_seconds=settings.auth_token_ttl_seconds,
        )
    except (UnknownLoginError, WrongPasswordError, InvalidRegistrationError) as exc:
        _raise_http(exc)
        raise
    return LoginResponse(token=result.token, user_id=result.user.id, email=result.user.email)
