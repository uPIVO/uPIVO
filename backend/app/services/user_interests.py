"""Интересы пользователя: хранение, нормализация и конкурентная уникальность.

Один список на пользователя, общий для всех включённых источников. Порядок
чтения — по `id`; именно он участвует в отпечатке входных данных оценки.
Конкурентные POST/PATCH и сохранение персональной оценки сериализуются
блокировкой строки `users`, поэтому предварительный SELECT не является
единственной защитой от дублей.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.ai.repository import ContentUnitAnalysisRepository
from app.models.user import User
from app.models.user_interest import UserInterest
from app.schemas.interest import InterestResponse
from app.services.users import require_user


class UnknownInterestError(LookupError):
    """У этого пользователя нет интереса с таким id."""


class DuplicateInterestError(ValueError):
    """Такой интерес у пользователя уже есть."""


class InvalidInterestError(ValueError):
    """Текст интереса пустой или не является строкой."""


def normalize_interest_text(text: str) -> str:
    """Убирает крайние пробелы и отвергает пустой текст."""
    if not isinstance(text, str):
        raise InvalidInterestError("Текст интереса должен быть строкой.")
    cleaned = text.strip()
    if not cleaned:
        raise InvalidInterestError("Текст интереса не должен быть пустым.")
    return cleaned


def read_interest_texts(session: Session, user_id: int) -> list[str]:
    """Детерминированный полный список интересов пользователя."""
    return list(
        session.scalars(
            select(UserInterest.text)
            .where(UserInterest.user_id == user_id)
            .order_by(UserInterest.id)
        )
    )


def list_interests(session: Session, user_id: int) -> list[InterestResponse]:
    require_user(session, user_id)
    rows = session.scalars(
        select(UserInterest).where(UserInterest.user_id == user_id).order_by(UserInterest.id)
    )
    return [_response(row) for row in rows]


def create_interest(session: Session, user_id: int, text: str) -> InterestResponse:
    """Добавляет интерес. Изменение списка инвалидирует текущие оценки."""
    require_user(session, user_id)
    cleaned = normalize_interest_text(text)
    _lock_user(session, user_id)
    _reject_duplicate(session, user_id, cleaned)
    interest = UserInterest(user_id=user_id, text=cleaned, created_at=_now())
    try:
        with session.begin_nested():
            session.add(interest)
            session.flush()
    except IntegrityError:
        session.expunge(interest)
        raise DuplicateInterestError(cleaned) from None
    _invalidate_scores(session, user_id)
    _commit(session)
    _notify_change(user_id)
    return _response(interest)


def update_interest(
    session: Session, user_id: int, interest_id: int, text: str
) -> InterestResponse:
    """Заменяет текст интереса своего пользователя.

    PATCH с тем же нормализованным текстом ничего не меняет: ни `updated_at`,
    ни персональные оценки, ни фоновый пересчёт не затрагиваются.
    """
    require_user(session, user_id)
    cleaned = normalize_interest_text(text)
    _lock_user(session, user_id)
    interest = _require_interest(session, user_id, interest_id)
    if interest.text == cleaned:
        return _response(interest)
    _reject_duplicate(session, user_id, cleaned, exclude_id=interest_id)
    interest.text = cleaned
    interest.updated_at = _now()
    try:
        with session.begin_nested():
            session.flush()
    except IntegrityError:
        raise DuplicateInterestError(cleaned) from None
    _invalidate_scores(session, user_id)
    _commit(session)
    _notify_change(user_id)
    return _response(interest)


def delete_interest(session: Session, user_id: int, interest_id: int) -> None:
    """Удаляет интерес; удаление последнего тоже инвалидирует текущие оценки."""
    require_user(session, user_id)
    _lock_user(session, user_id)
    interest = _require_interest(session, user_id, interest_id)
    session.delete(interest)
    session.flush()
    _invalidate_scores(session, user_id)
    _commit(session)
    _notify_change(user_id)


def _require_interest(session: Session, user_id: int, interest_id: int) -> UserInterest:
    interest = session.scalar(
        select(UserInterest).where(
            UserInterest.id == interest_id, UserInterest.user_id == user_id
        )
    )
    if interest is None:
        raise UnknownInterestError(interest_id)
    return interest


def _reject_duplicate(
    session: Session, user_id: int, text: str, *, exclude_id: int | None = None
) -> None:
    """Предварительная проверка: регистр и крайние пробелы не создают новый интерес.

    Решение о дубле принимает БД уникальным индексом по `lower(btrim(text))`;
    эта проверка нужна только для понятного 409 без нарушения ограничения.
    """
    rows = session.execute(
        select(UserInterest.id, UserInterest.text).where(UserInterest.user_id == user_id)
    ).all()
    folded = text.casefold()
    for interest_id, existing_text in rows:
        if interest_id == exclude_id:
            continue
        if existing_text.casefold() == folded:
            raise DuplicateInterestError(text)


def _lock_user(session: Session, user_id: int) -> None:
    """Сериализует изменения списка и сохранение оценок этого пользователя."""
    session.execute(select(User.id).where(User.id == user_id).with_for_update())


def _invalidate_scores(session: Session, user_id: int) -> None:
    ContentUnitAnalysisRepository().invalidate_user_relevance(session, user_id=user_id)


def _notify_change(user_id: int) -> None:
    from app.services.user_relevance import notify_interests_changed

    notify_interests_changed(user_id)


def _response(interest: UserInterest) -> InterestResponse:
    return InterestResponse(
        id=interest.id,
        user_id=interest.user_id,
        text=interest.text,
        created_at=interest.created_at,
        updated_at=interest.updated_at,
    )


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _commit(session: Session) -> None:
    try:
        session.commit()
    except Exception:
        session.rollback()
        raise
