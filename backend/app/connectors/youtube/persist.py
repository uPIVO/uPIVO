from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.connectors.base import FetchResult
from app.models.enums import ProcessingStatus, SourceType
from app.models.feed_item import FeedItem
from app.models.source import Source


@dataclass(frozen=True)
class PersistStats:
    source_id: int
    inserted: int
    updated: int


def persist_fetch(
    session: Session,
    fetch: FetchResult,
    *,
    now: datetime | None = None,
    source: Source | None = None,
) -> PersistStats:
    stamp = now or datetime.now(UTC).replace(tzinfo=None)
    if source is None:
        source = session.scalar(
            select(Source).where(
                Source.type == SourceType.YOUTUBE,
                Source.external_id == fetch.source_external_id,
            )
        )
    if source is None:
        source = Source(
            type=SourceType.YOUTUBE,
            name=_clip(fetch.source_name, 255) or fetch.source_external_id,
            canonical_url=fetch.canonical_url,
            external_id=fetch.source_external_id,
            last_synced_at=stamp,
        )
        session.add(source)
        session.flush()
    else:
        source.name = _clip(fetch.source_name, 255) or source.name
        source.canonical_url = fetch.canonical_url
        if fetch.source_external_id:
            source.external_id = fetch.source_external_id
        source.last_synced_at = stamp

    existing_ids = [item.external_id for item in fetch.items]
    existing_rows = (
        session.scalars(
            select(FeedItem).where(
                FeedItem.source_id == source.id,
                FeedItem.external_id.in_(existing_ids),
            )
        ).all()
        if existing_ids
        else []
    )
    by_external_id = {row.external_id: row for row in existing_rows}

    inserted = 0
    updated = 0
    for item in fetch.items:
        current = by_external_id.get(item.external_id)
        if current is None:
            session.add(
                FeedItem(
                    source_id=source.id,
                    external_id=item.external_id,
                    title=item.title,
                    body_text=item.body_text,
                    original_url=item.original_url,
                    author=_clip(item.author, 255),
                    published_at=item.published_at,
                    collected_at=stamp,
                    raw_payload=item.raw_payload,
                    processing_status=ProcessingStatus.PENDING,
                )
            )
            inserted += 1
        else:
            current.title = item.title
            current.body_text = item.body_text
            current.original_url = item.original_url
            current.author = _clip(item.author, 255)
            current.published_at = item.published_at
            current.raw_payload = item.raw_payload
            current.updated_at = stamp
            updated += 1
    return PersistStats(source_id=source.id, inserted=inserted, updated=updated)


def _clip(value: str | None, size: int) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    if not stripped:
        return None
    return stripped[:size]
