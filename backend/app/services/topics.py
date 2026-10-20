"""Thematic folders over the shared feed.

A folder classifies feed_items. It does not copy them and it is not favorites.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.feed_item import FeedItem
from app.models.feed_item_folder import FeedItemFolder
from app.models.folder import Folder
from app.schemas.catalog import TopicResponse
from app.services.users import require_user


class UnknownTopicError(LookupError):
    """No folder with this id belongs to the user."""


class UnknownFeedItemError(LookupError):
    """No shared feed item has this id."""


class TopicItemNotLinkedError(LookupError):
    """The feed item is not in this topic."""


class InvalidTopicError(ValueError):
    """The topic change is incomplete."""


def list_topics(session: Session, user_id: int) -> list[TopicResponse]:
    require_user(session, user_id)
    folders = session.scalars(
        select(Folder).where(Folder.user_id == user_id).order_by(Folder.id)
    ).all()
    item_ids = _item_ids_by_folder(session, [folder.id for folder in folders])
    return [_topic_response(folder, item_ids.get(folder.id, [])) for folder in folders]


def create_topic(
    session: Session,
    user_id: int,
    name: str,
    description: str | None,
    parent_id: int | None,
) -> TopicResponse:
    require_user(session, user_id)
    if parent_id is not None:
        _owned_topic(session, user_id, parent_id)
    folder = Folder(
        user_id=user_id,
        name=_clean_name(name),
        description=_clean_description(description),
        parent_id=parent_id,
        created_at=_now(),
    )
    session.add(folder)
    session.flush()
    response = _topic_response(folder, [])
    _commit(session)
    return response


def update_topic(
    session: Session,
    user_id: int,
    topic_id: int,
    *,
    name: str | None,
    description: str | None,
    update_description: bool,
) -> TopicResponse:
    require_user(session, user_id)
    if name is None and not update_description:
        raise InvalidTopicError("Nothing to update")
    folder = _owned_topic(session, user_id, topic_id)
    if name is not None:
        folder.name = _clean_name(name)
    if update_description:
        folder.description = _clean_description(description)
    folder.updated_at = _now()
    response = _topic_response(folder, _item_ids(session, folder.id))
    _commit(session)
    return response


def attach_feed_item(
    session: Session, user_id: int, topic_id: int, feed_item_id: int
) -> tuple[TopicResponse, bool]:
    """Put a shared feed item in the topic. Returns the topic and whether the link is new."""
    require_user(session, user_id)
    folder = _owned_topic(session, user_id, topic_id)
    if session.scalar(select(FeedItem.id).where(FeedItem.id == feed_item_id)) is None:
        raise UnknownFeedItemError(feed_item_id)
    created = _link_item(session, folder.id, feed_item_id)
    response = _topic_response(folder, _item_ids(session, folder.id))
    _commit(session)
    return response, created


def detach_feed_item(session: Session, user_id: int, topic_id: int, feed_item_id: int) -> None:
    require_user(session, user_id)
    folder = _owned_topic(session, user_id, topic_id)
    link = session.scalar(
        select(FeedItemFolder).where(
            FeedItemFolder.folder_id == folder.id,
            FeedItemFolder.feed_item_id == feed_item_id,
        )
    )
    if link is None:
        raise TopicItemNotLinkedError(feed_item_id)
    session.delete(link)
    _commit(session)


def _owned_topic(session: Session, user_id: int, topic_id: int) -> Folder:
    folder = session.scalar(select(Folder).where(Folder.id == topic_id, Folder.user_id == user_id))
    if folder is None:
        raise UnknownTopicError(topic_id)
    return folder


def _link_item(session: Session, folder_id: int, feed_item_id: int) -> bool:
    existing = session.scalar(
        select(FeedItemFolder.id).where(
            FeedItemFolder.folder_id == folder_id,
            FeedItemFolder.feed_item_id == feed_item_id,
        )
    )
    if existing is not None:
        return False
    link = FeedItemFolder(folder_id=folder_id, feed_item_id=feed_item_id, created_at=_now())
    try:
        with session.begin_nested():
            session.add(link)
            session.flush()
    except IntegrityError:
        session.expunge(link)
        still_missing = session.scalar(
            select(FeedItemFolder.id).where(
                FeedItemFolder.folder_id == folder_id,
                FeedItemFolder.feed_item_id == feed_item_id,
            )
        )
        if still_missing is None:
            raise
        return False
    return True


def _item_ids(session: Session, folder_id: int) -> list[int]:
    return _item_ids_by_folder(session, [folder_id]).get(folder_id, [])


def _item_ids_by_folder(session: Session, folder_ids: list[int]) -> dict[int, list[int]]:
    if not folder_ids:
        return {}
    rows = session.execute(
        select(FeedItemFolder.folder_id, FeedItemFolder.feed_item_id)
        .where(FeedItemFolder.folder_id.in_(folder_ids))
        .order_by(FeedItemFolder.id)
    ).all()
    grouped: dict[int, list[int]] = {}
    for folder_id, feed_item_id in rows:
        grouped.setdefault(folder_id, []).append(feed_item_id)
    return grouped


def _topic_response(folder: Folder, feed_item_ids: list[int]) -> TopicResponse:
    return TopicResponse(
        id=folder.id,
        user_id=folder.user_id,
        name=folder.name,
        description=folder.description,
        parent_id=folder.parent_id,
        feed_item_ids=feed_item_ids,
        created_at=folder.created_at,
        updated_at=folder.updated_at,
    )


def _clean_name(name: str) -> str:
    cleaned = name.strip()
    if not cleaned:
        raise InvalidTopicError("Topic name is required")
    return cleaned[:255]


def _clean_description(description: str | None) -> str | None:
    if description is None:
        return None
    cleaned = description.strip()
    return cleaned or None


def _commit(session: Session) -> None:
    try:
        session.commit()
    except Exception:
        session.rollback()
        raise


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)
