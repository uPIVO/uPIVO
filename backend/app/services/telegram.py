"""Сценарий загрузки и атомарного сохранения публикаций Telegram."""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from app.connectors.telegram.models import ResolvedTelegramChannel
from app.models import Source, SourceType
from app.models.feed_item import FeedItem
from app.models.telegram_sync_state import TelegramSyncState
from app.processing.telegram import map_channel, map_post


class TelegramSyncError(RuntimeError):
    """Безопасная ошибка сценария синхронизации, которую можно показать оператору."""


def _saved_cursor(session: Session, source_external_id: str) -> int | None:
    statement = (
        select(TelegramSyncState.last_message_id)
        .join(Source, Source.id == TelegramSyncState.source_id)
        .where(Source.type == SourceType.TELEGRAM, Source.external_id == source_external_id)
    )
    return session.execute(statement).scalar_one_or_none()


async def sync_telegram_channel(
    channel: ResolvedTelegramChannel,
    connector: Any,
    session_factory: sessionmaker[Session],
    *,
    limit: int | None = None,
) -> tuple[int, int]:
    """Загрузить публикации вне транзакции и сохранить их вместе с курсором."""
    source_values = map_channel(channel)
    external_id = source_values["external_id"]
    with session_factory() as session:
        starting_cursor = _saved_cursor(session, external_id)

    if starting_cursor is None:
        posts = await connector.fetch_latest_posts(channel, limit=50 if limit is None else limit)
    else:
        posts = await connector.fetch_new_posts(
            channel, after_message_id=starting_cursor, limit=limit
        )

    maximum_id = max(
        (max(post.message_ids, default=post.message_id) for post in posts),
        default=starting_cursor or 0,
    )
    with session_factory.begin() as session:
        source_insert = insert(Source).values(**source_values)
        source_upsert = source_insert.on_conflict_do_update(
            index_elements=[Source.type, Source.external_id],
            index_where=text("type = 'telegram'"),
            set_={
                "name": source_insert.excluded.name,
                "canonical_url": source_insert.excluded.canonical_url,
            },
        ).returning(Source.id)
        source_id = session.execute(source_upsert).scalar_one()

        state_insert = insert(TelegramSyncState).values(source_id=source_id)
        session.execute(state_insert.on_conflict_do_nothing(index_elements=[TelegramSyncState.source_id]))
        actual_cursor = session.execute(
            select(TelegramSyncState.last_message_id)
            .where(TelegramSyncState.source_id == source_id)
            .with_for_update()
        ).scalar_one()
        expected_cursor = starting_cursor or 0
        if actual_cursor != expected_cursor:
            raise TelegramSyncError(
                "Курсор канала изменился во время загрузки. Повторите запуск; "
                "публикации не сохранены."
            )

        for post in posts:
            item_values = map_post(channel, post)
            item_values["source_id"] = source_id
            item_insert = insert(FeedItem).values(**item_values)
            session.execute(
                item_insert.on_conflict_do_update(
                    index_elements=[FeedItem.source_id, FeedItem.external_id],
                    set_={
                        "body_text": item_insert.excluded.body_text,
                        "original_url": item_insert.excluded.original_url,
                        "published_at": item_insert.excluded.published_at,
                        "raw_payload": item_insert.excluded.raw_payload,
                    },
                )
            )
        if maximum_id > actual_cursor:
            session.execute(
                update(TelegramSyncState)
                .where(TelegramSyncState.source_id == source_id)
                .values(last_message_id=maximum_id)
            )
        session.execute(
            update(Source)
            .where(Source.id == source_id)
            .values(last_synced_at=datetime.now(UTC).replace(tzinfo=None))
        )

    return len(posts), max(starting_cursor or 0, maximum_id)
