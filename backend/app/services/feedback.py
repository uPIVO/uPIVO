"""Обратная связь пользователя по публикациям и инфоповодам.

Одна текущая реакция на пару «пользователь + единица». Запись и удаление
сериализуются блокировкой строки `users` (как `user_interests._lock_user`):
проверка и изменение идут в одной транзакции, поэтому конкурентные PUT не
создают дубликат и не нарушают UNIQUE. Реакции не меняют ранжирование,
интересы, AI-оценки и не запускают фоновые задачи.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime

from sqlalchemy import or_, select
from sqlalchemy.orm import InstrumentedAttribute, Session

from app.models.content_cluster import ContentCluster
from app.models.enums import FeedbackType
from app.models.feed_item import FeedItem
from app.models.feedback import Feedback
from app.models.user import User
from app.schemas.feedback import FeedbackKind, FeedbackResponse
from app.services.users import require_user
from app.services.visibility import cluster_visible, standalone_feed_item_visible


class UnknownFeedbackError(LookupError):
    """У пользователя нет текущей реакции на эту единицу."""


class UnavailableFeedbackTargetError(LookupError):
    """Единица не существует или сейчас недоступна по правилам показа."""


_TARGET_COLUMNS = {
    FeedbackKind.FEED_ITEM: Feedback.feed_item_id,
    FeedbackKind.CLUSTER: Feedback.cluster_id,
}


def set_feedback(
    session: Session,
    user_id: int,
    kind: FeedbackKind,
    unit_id: int,
    reaction: FeedbackType,
) -> tuple[FeedbackResponse, bool]:
    """Создаёт или заменяет текущую реакцию; `True` — строка создана впервые.

    Блокировка строки пользователя берётся до чтения реакции, поэтому проверка
    доступности, чтение и запись остаются в одной транзакции. Совпадающая
    реакция не меняет `updated_at` и ничего не пишет.
    """
    require_user(session, user_id)
    _lock_user(session, user_id)
    _require_visible_target(session, user_id, kind, unit_id)
    existing = _find(session, user_id, kind, unit_id)
    if existing is None:
        feedback = Feedback(
            user_id=user_id,
            reaction=reaction,
            created_at=_now(),
            **_target(kind, unit_id),
        )
        session.add(feedback)
        _commit(session)
        return _response(feedback), True
    if existing.reaction != reaction:
        existing.reaction = reaction
        existing.updated_at = _now()
    _commit(session)
    return _response(existing), False


def get_feedback(
    session: Session, user_id: int, kind: FeedbackKind, unit_id: int
) -> FeedbackResponse:
    """Текущая реакция пользователя; доступность единицы не требуется."""
    require_user(session, user_id)
    feedback = _find(session, user_id, kind, unit_id)
    if feedback is None:
        raise UnknownFeedbackError(unit_id)
    return _response(feedback)


def delete_feedback(session: Session, user_id: int, kind: FeedbackKind, unit_id: int) -> None:
    """Физически удаляет реакцию; повторное удаление — штатное состояние.

    Отключение источника не мешает удалить собственную прежнюю реакцию.
    """
    require_user(session, user_id)
    _lock_user(session, user_id)
    feedback = _find(session, user_id, kind, unit_id)
    if feedback is not None:
        session.delete(feedback)
    _commit(session)


def list_feedback(
    session: Session,
    user_id: int,
    kind: FeedbackKind | None = None,
    unit_ids: Iterable[int] | None = None,
    limit: int = 20,
    offset: int = 0,
) -> list[FeedbackResponse]:
    """Реакции пользователя по `created_at DESC, id DESC`.

    С `unit_ids` возвращаются реакции на запрошенные ID одним запросом и без
    пагинации: единицы без реакции просто отсутствуют в ответе.
    """
    require_user(session, user_id)
    statement = select(Feedback).where(Feedback.user_id == user_id)
    if unit_ids is not None:
        if kind is None:
            raise ValueError("unit_ids допустим только вместе с kind")
        unique_ids = list(dict.fromkeys(unit_ids))
        if not unique_ids:
            return []
        statement = statement.where(_target_column(kind).in_(unique_ids))
    elif kind is not None:
        statement = statement.where(_target_column(kind).is_not(None))
    statement = statement.order_by(Feedback.created_at.desc(), Feedback.id.desc())
    if unit_ids is None:
        statement = statement.limit(limit).offset(offset)
    return [_response(row) for row in session.scalars(statement)]


def read_reactions(
    session: Session,
    user_id: int,
    pairs: Iterable[tuple[FeedbackKind, int]],
) -> dict[tuple[FeedbackKind, int], FeedbackType]:
    """Текущие реакции по парам `(kind, unit_id)` одним запросом, без N+1.

    Внутреннее чтение для будущей персонализации: отсутствие пары означает
    «сигнала нет», а не отрицательную реакцию. Существование пользователя здесь
    не проверяется.
    """
    requested = list(dict.fromkeys((FeedbackKind(kind), unit_id) for kind, unit_id in pairs))
    if not requested:
        return {}
    feed_item_ids = [unit_id for kind, unit_id in requested if kind == FeedbackKind.FEED_ITEM]
    cluster_ids = [unit_id for kind, unit_id in requested if kind == FeedbackKind.CLUSTER]
    conditions = []
    if feed_item_ids:
        conditions.append(Feedback.feed_item_id.in_(feed_item_ids))
    if cluster_ids:
        conditions.append(Feedback.cluster_id.in_(cluster_ids))
    rows = session.scalars(
        select(Feedback).where(Feedback.user_id == user_id, or_(*conditions))
    )
    requested_set = set(requested)
    found: dict[tuple[FeedbackKind, int], FeedbackType] = {}
    for row in rows:
        pair = (_kind(row), _unit_id(row))
        if pair in requested_set:
            found[pair] = row.reaction
    return found


def _require_visible_target(
    session: Session, user_id: int, kind: FeedbackKind, unit_id: int
) -> None:
    """Проверяет возможность показа единицы, не раскрывая причину отказа."""
    if kind == FeedbackKind.FEED_ITEM:
        statement = select(FeedItem.id).where(
            FeedItem.id == unit_id, standalone_feed_item_visible(user_id)
        )
    elif kind == FeedbackKind.CLUSTER:
        statement = select(ContentCluster.id).where(
            ContentCluster.id == unit_id, cluster_visible(user_id)
        )
    else:
        raise ValueError(f"Неизвестный тип единицы: {kind!r}")
    if session.scalar(statement) is None:
        raise UnavailableFeedbackTargetError(unit_id)


def _find(
    session: Session, user_id: int, kind: FeedbackKind, unit_id: int
) -> Feedback | None:
    return session.scalar(
        select(Feedback).where(Feedback.user_id == user_id, _target_column(kind) == unit_id)
    )


def _target_column(kind: FeedbackKind) -> InstrumentedAttribute[int | None]:
    """Колонка единицы для типа; неизвестный тип — ошибка, а не молчаливый выбор."""
    try:
        return _TARGET_COLUMNS[FeedbackKind(kind)]
    except (KeyError, ValueError) as exc:
        raise ValueError(f"Неизвестный тип единицы: {kind!r}") from exc


def _target(kind: FeedbackKind, unit_id: int) -> dict[str, int]:
    return {_target_column(kind).key: unit_id}


def _kind(feedback: Feedback) -> FeedbackKind:
    return FeedbackKind.FEED_ITEM if feedback.feed_item_id is not None else FeedbackKind.CLUSTER


def _unit_id(feedback: Feedback) -> int:
    if feedback.feed_item_id is not None:
        return feedback.feed_item_id
    assert feedback.cluster_id is not None, "CHECK feedback требует ровно одну единицу"
    return feedback.cluster_id


def _response(feedback: Feedback) -> FeedbackResponse:
    return FeedbackResponse(
        id=feedback.id,
        user_id=feedback.user_id,
        kind=_kind(feedback),
        unit_id=_unit_id(feedback),
        reaction=feedback.reaction,
        created_at=feedback.created_at,
        updated_at=feedback.updated_at,
    )


def _lock_user(session: Session, user_id: int) -> None:
    """Сериализует изменения реакций этого пользователя."""
    session.execute(select(User.id).where(User.id == user_id).with_for_update())


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _commit(session: Session) -> None:
    try:
        session.commit()
    except Exception:
        session.rollback()
        raise
