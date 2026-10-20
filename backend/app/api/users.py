"""Per-user sources and topics.

Authentication is not part of this API. The path user_id is users.id and must
already exist. This module does not add JWT, Firebase, or OAuth.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Response
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.schemas.catalog import (
    SourceAttachRequest,
    SourceEnabledRequest,
    SourceLinkResponse,
    TopicCreateRequest,
    TopicItemRequest,
    TopicResponse,
    TopicUpdateRequest,
)
from app.services.topics import (
    InvalidTopicError,
    TopicItemNotLinkedError,
    UnknownFeedItemError,
    UnknownTopicError,
    attach_feed_item,
    create_topic,
    detach_feed_item,
    list_topics,
    update_topic,
)
from app.services.user_sources import (
    InvalidSourceAttachError,
    UnknownSourceLinkError,
    attach_source,
    list_sources,
    set_source_enabled,
)
from app.services.users import UnknownUserError

_AUTH = "Авторизация в этот API не входит: user_id — это существующий users.id."

sources_router = APIRouter(prefix="/users/{user_id}/sources", tags=["sources"])
topics_router = APIRouter(prefix="/users/{user_id}/topics", tags=["topics"])


def _raise_http(exc: Exception) -> None:
    if isinstance(exc, UnknownUserError):
        raise HTTPException(status_code=404, detail="Unknown user") from exc
    if isinstance(exc, UnknownSourceLinkError):
        raise HTTPException(status_code=404, detail="Unknown source") from exc
    if isinstance(exc, UnknownTopicError):
        raise HTTPException(status_code=404, detail="Unknown topic") from exc
    if isinstance(exc, UnknownFeedItemError):
        raise HTTPException(status_code=404, detail="Unknown feed item") from exc
    if isinstance(exc, TopicItemNotLinkedError):
        raise HTTPException(status_code=404, detail="Feed item is not in this topic") from exc
    if isinstance(exc, InvalidSourceAttachError | InvalidTopicError):
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    raise exc


@sources_router.get(
    "",
    response_model=list[SourceLinkResponse],
    summary="Список источников пользователя",
    description=f"Связи user_sources, включая выключенные. {_AUTH}",
)
def get_sources(
    user_id: Annotated[int, Path(ge=1)],
    session: Annotated[Session, Depends(get_db)],
) -> list[SourceLinkResponse]:
    try:
        return list_sources(session, user_id)
    except UnknownUserError as exc:
        _raise_http(exc)
        raise


@sources_router.post(
    "",
    response_model=SourceLinkResponse,
    status_code=201,
    summary="Подключить источник",
    description=(
        "Ищет или создаёт одну общую строку sources и включает её через user_sources. "
        "Для YouTube достаточно URL канала с id UC…. Канал при этом не скачивается. "
        f"{_AUTH}"
    ),
)
def post_source(
    user_id: Annotated[int, Path(ge=1)],
    payload: SourceAttachRequest,
    response: Response,
    session: Annotated[Session, Depends(get_db)],
) -> SourceLinkResponse:
    try:
        body, created = attach_source(session, user_id, payload.type, payload.url, payload.name)
    except (UnknownUserError, InvalidSourceAttachError) as exc:
        _raise_http(exc)
        raise
    if not created:
        response.status_code = 200
    return body


@sources_router.patch(
    "/{source_id}",
    response_model=SourceLinkResponse,
    summary="Включить или выключить источник",
    description=f"Меняет user_sources.enabled. Общая строка sources остаётся. {_AUTH}",
)
def patch_source(
    user_id: Annotated[int, Path(ge=1)],
    source_id: Annotated[int, Path(ge=1)],
    payload: SourceEnabledRequest,
    session: Annotated[Session, Depends(get_db)],
) -> SourceLinkResponse:
    try:
        return set_source_enabled(session, user_id, source_id, payload.enabled)
    except (UnknownUserError, UnknownSourceLinkError) as exc:
        _raise_http(exc)
        raise


@topics_router.get(
    "",
    response_model=list[TopicResponse],
    summary="Список тем пользователя",
    description=(
        "Папки folders классифицируют общую ленту. В ответе только id публикаций, "
        f"без копии feed_items. {_AUTH}"
    ),
)
def get_topics(
    user_id: Annotated[int, Path(ge=1)],
    session: Annotated[Session, Depends(get_db)],
) -> list[TopicResponse]:
    try:
        return list_topics(session, user_id)
    except UnknownUserError as exc:
        _raise_http(exc)
        raise


@topics_router.post(
    "",
    response_model=TopicResponse,
    status_code=201,
    summary="Создать тему",
    description=f"Создаёт папку пользователя. Это не избранное и не отдельная лента. {_AUTH}",
)
def post_topic(
    user_id: Annotated[int, Path(ge=1)],
    payload: TopicCreateRequest,
    session: Annotated[Session, Depends(get_db)],
) -> TopicResponse:
    try:
        return create_topic(session, user_id, payload.name, payload.description, payload.parent_id)
    except (UnknownUserError, UnknownTopicError, InvalidTopicError) as exc:
        _raise_http(exc)
        raise


@topics_router.patch(
    "/{topic_id}",
    response_model=TopicResponse,
    summary="Переименовать тему",
    description=f"Меняет название или описание папки. {_AUTH}",
)
def patch_topic(
    user_id: Annotated[int, Path(ge=1)],
    topic_id: Annotated[int, Path(ge=1)],
    payload: TopicUpdateRequest,
    session: Annotated[Session, Depends(get_db)],
) -> TopicResponse:
    try:
        return update_topic(
            session,
            user_id,
            topic_id,
            name=payload.name,
            description=payload.description,
            update_description="description" in payload.model_fields_set,
        )
    except (UnknownUserError, UnknownTopicError, InvalidTopicError) as exc:
        _raise_http(exc)
        raise


@topics_router.post(
    "/{topic_id}/items",
    response_model=TopicResponse,
    status_code=201,
    summary="Положить публикацию в тему",
    description=(
        "Добавляет связь feed_item_folders. Публикация остаётся в общей таблице feed_items. "
        f"{_AUTH}"
    ),
)
def post_topic_item(
    user_id: Annotated[int, Path(ge=1)],
    topic_id: Annotated[int, Path(ge=1)],
    payload: TopicItemRequest,
    response: Response,
    session: Annotated[Session, Depends(get_db)],
) -> TopicResponse:
    try:
        body, created = attach_feed_item(session, user_id, topic_id, payload.feed_item_id)
    except (UnknownUserError, UnknownTopicError, UnknownFeedItemError) as exc:
        _raise_http(exc)
        raise
    if not created:
        response.status_code = 200
    return body


@topics_router.delete(
    "/{topic_id}/items/{feed_item_id}",
    status_code=204,
    summary="Убрать публикацию из темы",
    description=f"Удаляет только связь feed_item_folders. {_AUTH}",
)
def delete_topic_item(
    user_id: Annotated[int, Path(ge=1)],
    topic_id: Annotated[int, Path(ge=1)],
    feed_item_id: Annotated[int, Path(ge=1)],
    session: Annotated[Session, Depends(get_db)],
) -> Response:
    try:
        detach_feed_item(session, user_id, topic_id, feed_item_id)
    except (UnknownUserError, UnknownTopicError, TopicItemNotLinkedError) as exc:
        _raise_http(exc)
    return Response(status_code=204)
