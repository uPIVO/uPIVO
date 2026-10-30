"""Create a user and check email plus password. Does not guard other APIs."""

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth.email import normalize_email
from app.auth.passwords import hash_password, verify_password
from app.auth.tokens import issue_token
from app.models.user import User


class InvalidRegistrationError(ValueError):
    """Email or password cannot be stored."""


class DuplicateEmailError(Exception):
    """users.email is already taken."""


class UnknownLoginError(LookupError):
    """No users row has this email."""


class WrongPasswordError(Exception):
    """The password does not match password_hash."""


@dataclass(frozen=True)
class LoginResult:
    token: str
    user: User


def register_user(session: Session, email: str, password: str) -> User:
    """Insert users.email and a scrypt password_hash. The password is not stored."""
    normalized = _email(email)
    if not 8 <= len(password) <= 128:
        raise InvalidRegistrationError("Password must be 8 to 128 characters")
    if session.scalar(select(User.id).where(User.email == normalized)) is not None:
        raise DuplicateEmailError(normalized)
    user = User(email=normalized, password_hash=hash_password(password), created_at=_now())
    session.add(user)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise DuplicateEmailError(normalized) from None
    session.refresh(user)
    return user


def login_user(
    session: Session,
    email: str,
    password: str,
    *,
    secret: str,
    ttl_seconds: int,
) -> LoginResult:
    """Check the password and return a signed token for this user."""
    normalized = _email(email)
    user = session.scalar(select(User).where(User.email == normalized))
    if user is None:
        raise UnknownLoginError(normalized)
    if not verify_password(password, user.password_hash):
        raise WrongPasswordError(normalized)
    token = issue_token(user_id=user.id, secret=secret, ttl_seconds=ttl_seconds)
    return LoginResult(token=token, user=user)


def _email(value: str) -> str:
    try:
        return normalize_email(value)
    except ValueError as exc:
        raise InvalidRegistrationError("Invalid email") from exc


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)
