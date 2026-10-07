from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

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


@dataclass(frozen=True)
class StoredTranscript:
    text: str
    lang: str | None = None
    kind: str | None = None


def persist_fetch(
    session: Session, fetch: FetchResult, *, now: datetime | None = None
) -> PersistStats:
    stamp = now or datetime.now(UTC).replace(tzinfo=None)
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
        payload = _merge_payload(item.raw_payload, current)
        stored = _transcript_from_payload(payload, item.body_text)
        body_text = stored.text if stored is not None else item.body_text
        if current is None:
            session.add(
                FeedItem(
                    source_id=source.id,
                    external_id=item.external_id,
                    title=item.title,
                    body_text=body_text,
                    original_url=item.original_url,
                    author=_clip(item.author, 255),
                    published_at=item.published_at,
                    collected_at=stamp,
                    raw_payload=payload,
                    processing_status=ProcessingStatus.PENDING,
                )
            )
            inserted += 1
        else:
            current.title = item.title
            current.body_text = body_text
            current.original_url = item.original_url
            current.author = _clip(item.author, 255)
            current.published_at = item.published_at
            current.raw_payload = payload
            current.updated_at = stamp
            updated += 1
    return PersistStats(source_id=source.id, inserted=inserted, updated=updated)


def load_existing_transcripts(
    session: Session, source_external_id: str, video_ids: list[str]
) -> dict[str, StoredTranscript]:
    """Map video id → stored captions for this YouTube source, if any."""
    if not video_ids:
        return {}
    source = session.scalar(
        select(Source).where(
            Source.type == SourceType.YOUTUBE,
            Source.external_id == source_external_id,
        )
    )
    if source is None:
        return {}
    rows = session.scalars(
        select(FeedItem).where(
            FeedItem.source_id == source.id,
            FeedItem.external_id.in_(video_ids),
        )
    ).all()
    found: dict[str, StoredTranscript] = {}
    for row in rows:
        stored = transcript_from_row(row)
        if stored is not None:
            found[row.external_id] = stored
    return found


def transcript_from_row(row: FeedItem) -> StoredTranscript | None:
    payload = row.raw_payload if isinstance(row.raw_payload, dict) else {}
    return _transcript_from_payload(payload, row.body_text)


def _transcript_from_payload(
    payload: dict[str, Any], body_text: str | None
) -> StoredTranscript | None:
    text = payload.get("transcript")
    if isinstance(text, str) and text.strip():
        lang = payload.get("transcript_lang")
        kind = payload.get("transcript_kind")
        return StoredTranscript(
            text=text.strip(),
            lang=str(lang) if isinstance(lang, str) and lang.strip() else None,
            kind=str(kind) if isinstance(kind, str) and kind.strip() else None,
        )
    if payload.get("transcript_lang") or payload.get("transcript_kind"):
        body = (body_text or "").strip()
        if body:
            lang = payload.get("transcript_lang")
            kind = payload.get("transcript_kind")
            return StoredTranscript(
                text=body,
                lang=str(lang) if isinstance(lang, str) and lang.strip() else None,
                kind=str(kind) if isinstance(kind, str) and kind.strip() else None,
            )
    return None


def _merge_payload(
    item_payload: dict[str, Any] | None, existing: FeedItem | None
) -> dict[str, Any]:
    payload = dict(item_payload or {})
    if _transcript_from_payload(payload, None) is not None:
        return payload
    if existing is None:
        return payload
    stored = transcript_from_row(existing)
    if stored is None:
        return payload
    payload["transcript"] = stored.text
    if stored.lang:
        payload["transcript_lang"] = stored.lang
    if stored.kind:
        payload["transcript_kind"] = stored.kind
    return payload


def _clip(value: str | None, size: int) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    if not stripped:
        return None
    return stripped[:size]
