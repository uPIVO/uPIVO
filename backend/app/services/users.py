"""Load a users row. Login is not implemented."""

from sqlalchemy.orm import Session

from app.models.user import User


class UnknownUserError(LookupError):
    """No users row has this id."""


def require_user(session: Session, user_id: int) -> User:
    user = session.get(User, user_id)
    if user is None:
        raise UnknownUserError(user_id)
    return user
