"""Shared signed Bearer authentication. No development identity fallbacks."""

from typing import Annotated

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.auth.tokens import InvalidTokenError, read_user_id
from app.core.config import get_settings
from app.db.session import get_db
from app.models.user import User

security = HTTPBearer(auto_error=False)


def get_auth_secret() -> str:
    secret = get_settings().auth_token_secret.get_secret_value()
    if len(secret) < 32 or secret == "dev-only-change-me":
        raise HTTPException(status_code=503, detail="Authentication is not configured")
    return secret


def get_current_user_id(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(security)],
    db: Annotated[Session, Depends(get_db)],
) -> int:
    if credentials is None:
        raise _unauthorized()
    try:
        user_id = read_user_id(credentials.credentials, get_auth_secret())
    except InvalidTokenError as exc:
        raise _unauthorized() from exc
    if db.get(User, user_id) is None:
        raise _unauthorized()
    return user_id


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=401, detail="Not authenticated", headers={"WWW-Authenticate": "Bearer"}
    )
