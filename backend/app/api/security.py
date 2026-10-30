"""Ownership checks for compatibility APIs that still accept a user_id."""

from typing import Annotated

from fastapi import Depends, HTTPException, Path, Query

from app.core.auth import get_current_user_id


def check_user(user_id: int, current_user_id: int) -> None:
    if user_id != current_user_id:
        raise HTTPException(status_code=403, detail="Forbidden")


def require_same_user(
    user_id: Annotated[int, Path(ge=1)],
    current_user_id: Annotated[int, Depends(get_current_user_id)],
) -> None:
    check_user(user_id, current_user_id)


def require_query_user(
    user_id: Annotated[int, Query(ge=1)],
    current_user_id: Annotated[int, Depends(get_current_user_id)],
) -> None:
    check_user(user_id, current_user_id)
