"""Bearer check for /users/{user_id} routes. Register, login, and health stay public."""

from typing import Annotated

from fastapi import Header, HTTPException, Path

from app.auth.tokens import InvalidTokenError, read_user_id
from app.core.config import get_settings


def require_same_user(
    user_id: Annotated[int, Path(ge=1)],
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    """Reject a missing or bad token, and a token that belongs to another user."""
    token = _bearer(authorization)
    if token is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        subject = read_user_id(token, get_settings().auth_token_secret.get_secret_value())
    except InvalidTokenError as exc:
        raise HTTPException(status_code=401, detail="Not authenticated") from exc
    if subject != user_id:
        raise HTTPException(status_code=403, detail="Forbidden")


def _bearer(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, value = authorization.partition(" ")
    token = value.strip()
    if scheme.lower() != "bearer" or not token:
        return None
    return token
