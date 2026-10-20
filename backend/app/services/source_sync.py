"""Synchronize each subscribed source once into shared feed_items."""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.connectors.habr.client import HabrClient
from app.connectors.telegram.client import TelegramConnector
from app.connectors.youtube import DEFAULT_LIMIT, collect_channel
from app.db.session import get_engine
from app.models.enums import SourceType
from app.models.source import Source
from app.models.user_source import UserSource
from app.processing.telegram import map_channel
from app.services.habr import HabrService
from app.services.reddit import sync_reddit
from app.services.telegram import sync_telegram_channel

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SourceSyncResult:
    source_id: int
    source_type: str
    status: str
    fetched: int = 0
    saved: int = 0
    inserted: int | None = None
    updated: int | None = None
    reason: str | None = None


def _session_factory(factory: sessionmaker[Session] | None) -> sessionmaker[Session]:
    return factory if factory is not None else sessionmaker(bind=get_engine())


async def sync_source(
    source_id: int,
    *,
    session_factory: sessionmaker[Session] | None = None,
    limit: int | None = None,
) -> SourceSyncResult:
    """Run the existing collector service for one catalog source."""
    if limit is not None and limit < 1:
        raise ValueError("limit must be >= 1")
    factory = _session_factory(session_factory)
    with factory() as session:
        source = session.get(Source, source_id)
        if source is None:
            raise ValueError(f"Unknown source: {source_id}")
        source_type = source.type
        url = source.canonical_url
        external_id = source.external_id

    try:
        if source_type is SourceType.YOUTUBE:
            with factory() as session:
                source = session.get(Source, source_id)
                fetch, stats = collect_channel(
                    session, source.canonical_url, limit=limit or DEFAULT_LIMIT, source=source
                )
                session.commit()
            return SourceSyncResult(
                source_id, source_type.value, "collected" if fetch.items else "empty",
                fetched=len(fetch.items), saved=stats.inserted + stats.updated,
                inserted=stats.inserted, updated=stats.updated,
            )

        if source_type is SourceType.HABR:
            if url != HabrClient.RSS_URL:
                return SourceSyncResult(
                    source_id, source_type.value, "skipped", reason="unsupported Habr URL"
                )
            with factory() as session:
                stats = await HabrService(session).sync_publications()
            return SourceSyncResult(
                source_id, source_type.value, "collected" if stats.fetched else "empty",
                fetched=stats.fetched, saved=stats.saved, inserted=stats.saved, updated=0,
            )

        if source_type is SourceType.REDDIT:
            if not external_id:
                return SourceSyncResult(
                    source_id, source_type.value, "skipped", reason="missing subreddit"
                )
            stats = sync_reddit(factory, external_id, limit=limit)
            return SourceSyncResult(
                source_id, source_type.value, "collected" if stats.fetched else "empty",
                fetched=stats.fetched, saved=stats.saved, inserted=stats.saved, updated=0,
            )

        if source_type is SourceType.TELEGRAM:
            async with TelegramConnector() as connector:
                channel = await connector.resolve_channel(url)
                channel_values = map_channel(channel)
                if external_id is None:
                    with factory.begin() as session:
                        source = session.get(Source, source_id, with_for_update=True)
                        if source.external_id is None:
                            source.external_id = channel_values["external_id"]
                            source.canonical_url = channel_values["canonical_url"]
                            source.name = channel_values["name"]
                        external_id = source.external_id
                if external_id != channel_values["external_id"]:
                    return SourceSyncResult(
                        source_id, source_type.value, "failed", reason="Telegram source ID mismatch"
                    )
                fetched, _cursor = await sync_telegram_channel(
                    channel, connector, factory, limit=limit
                )
            # Telegram's existing service returns upsert count, not an insert/update split.
            return SourceSyncResult(
                source_id, source_type.value, "collected" if fetched else "empty",
                fetched=fetched, saved=fetched,
            )

        return SourceSyncResult(
            source_id, source_type.value, "skipped", reason="unsupported source type"
        )
    except Exception as exc:
        logger.exception("Source %s sync failed", source_id)
        return SourceSyncResult(source_id, source_type.value, "failed", reason=type(exc).__name__)


async def sync_active_sources(
    *,
    session_factory: sessionmaker[Session] | None = None,
    limit: int | None = None,
) -> list[SourceSyncResult]:
    """Sync DISTINCT sources with at least one enabled user link, once per run."""
    if limit is not None and limit < 1:
        raise ValueError("limit must be >= 1")
    factory = _session_factory(session_factory)
    with factory() as session:
        source_ids = session.scalars(
            select(Source.id)
            .join(UserSource, UserSource.source_id == Source.id)
            .where(UserSource.enabled.is_(True))
            .distinct()
            .order_by(Source.id)
        ).all()
    return await sync_sources(source_ids, session_factory=factory, limit=limit)


async def sync_sources(
    source_ids: Iterable[int],
    *,
    session_factory: sessionmaker[Session] | None = None,
    limit: int | None = None,
) -> list[SourceSyncResult]:
    """Sync each requested source once through the shared dispatcher."""
    if limit is not None and limit < 1:
        raise ValueError("limit must be >= 1")
    factory = _session_factory(session_factory)
    return [
        await sync_source(source_id, session_factory=factory, limit=limit)
        for source_id in sorted(set(source_ids))
    ]
