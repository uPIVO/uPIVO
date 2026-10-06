"""Map raw_payload onto the shared FeedItem fields and hash exact duplicates."""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.enums import ProcessingStatus
from app.models.feed_item import FeedItem
from app.processing.text import clean_oneline, clean_text, clip

logger = logging.getLogger(__name__)

AUTHOR_MAX_LEN = 255
YOUTUBE_RSS_KIND = "youtube_rss_entry"

_TITLE_KEYS = ("title", "headline", "name")
_BODY_KEYS = (
    "body_text",
    "body",
    "text",
    "content",
    "description",
    "summary",
    "message",
    "transcript",
)
_URL_KEYS = ("original_url", "url", "link", "canonical_url", "permalink")
_AUTHOR_KEYS = ("author", "author_name", "username")
_PUBLISHED_KEYS = ("published_at", "published", "pubDate", "date")


class FieldFallback(Protocol):
    title: str | None
    body_text: str
    original_url: str
    author: str | None
    published_at: datetime | None


@dataclass(frozen=True)
class NormalizedContent:
    title: str | None
    body_text: str
    original_url: str
    author: str | None
    published_at: datetime | None
    content_hash: str


@dataclass(frozen=True)
class NormalizeOutcome:
    ok: bool
    content: NormalizedContent | None
    reason: str | None = None


@dataclass(frozen=True)
class NormalizeStats:
    processed: int
    failed: int
    skipped: int


def content_hash(title: str | None, body_text: str) -> str:
    """SHA-256 of normalized title + body. Same cleaned text → same hash (exact dupes)."""
    material = f"{title or ''}\n{body_text}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def map_raw_payload(payload: Any, *, fallback: FieldFallback | None = None) -> NormalizeOutcome:
    """Turn a connector dump into unified FeedItem fields. Does not write the database."""
    if not isinstance(payload, dict):
        return NormalizeOutcome(ok=False, content=None, reason="raw_payload is not an object")

    title = clean_oneline(_pick_text(payload, _TITLE_KEYS))
    body = clean_text(_pick_body(payload))
    url = _clean_url(_pick_url(payload))
    author = clip(clean_oneline(_pick_text(payload, _AUTHOR_KEYS)), AUTHOR_MAX_LEN)
    published = _pick_published(payload)

    if fallback is not None:
        if title is None:
            title = clean_oneline(fallback.title)
        if not body:
            body = clean_text(fallback.body_text)
        if url is None:
            url = _clean_url(fallback.original_url)
        if author is None:
            author = clip(clean_oneline(fallback.author), AUTHOR_MAX_LEN)
        if published is None:
            published = _coerce_datetime(fallback.published_at)

    if not url:
        return NormalizeOutcome(ok=False, content=None, reason="original_url is missing")

    return NormalizeOutcome(
        ok=True,
        content=NormalizedContent(
            title=title,
            body_text=body,
            original_url=url,
            author=author,
            published_at=published,
            content_hash=content_hash(title, body),
        ),
    )


def normalize_feed_item(item: FeedItem, *, now: datetime | None = None) -> NormalizeOutcome:
    """Fill unified fields from raw_payload. Leaves raw_payload unchanged.

    Connectors may call this after persist. processing_status becomes processed or failed.
    extracted_entities / summary / importance_score are not touched.
    """
    stamp = now or datetime.now(UTC).replace(tzinfo=None)
    outcome = map_raw_payload(item.raw_payload, fallback=item)
    if not outcome.ok or outcome.content is None:
        item.processing_status = ProcessingStatus.FAILED
        item.updated_at = stamp
        logger.warning(
            "Feed item %s normalization failed: %s",
            getattr(item, "id", None),
            outcome.reason,
        )
        return outcome

    mapped = outcome.content
    item.title = mapped.title
    item.body_text = mapped.body_text
    item.original_url = mapped.original_url
    item.author = mapped.author
    item.published_at = mapped.published_at
    item.content_hash = mapped.content_hash
    item.processing_status = ProcessingStatus.PROCESSED
    item.updated_at = stamp
    return outcome


def list_pending_feed_items(
    session: Session,
    *,
    limit: int | None = None,
    item_id: int | None = None,
) -> list[FeedItem]:
    stmt = select(FeedItem)
    if item_id is not None:
        stmt = stmt.where(FeedItem.id == item_id)
    else:
        stmt = stmt.where(FeedItem.processing_status == ProcessingStatus.PENDING).order_by(
            FeedItem.id
        )
        if limit is not None:
            stmt = stmt.limit(limit)
    return list(session.scalars(stmt).all())


def normalize_pending(
    session: Session,
    *,
    limit: int | None = None,
    item_id: int | None = None,
    now: datetime | None = None,
) -> NormalizeStats:
    """Normalize pending feed_items. Caller commits. Skips non-pending rows."""
    items = list_pending_feed_items(session, limit=limit, item_id=item_id)
    processed = 0
    failed = 0
    skipped = 0
    for item in items:
        if item.processing_status != ProcessingStatus.PENDING:
            skipped += 1
            continue
        outcome = normalize_feed_item(item, now=now)
        if outcome.ok:
            processed += 1
        else:
            failed += 1
    return NormalizeStats(processed=processed, failed=failed, skipped=skipped)


def _pick_body(payload: dict[str, Any]) -> str | None:
    if payload.get("kind") == YOUTUBE_RSS_KIND:
        chosen = _as_text(payload.get("transcript")) or _as_text(payload.get("description"))
        if chosen:
            return chosen
    return _pick_text(payload, _BODY_KEYS)


def _pick_url(payload: dict[str, Any]) -> str | None:
    if payload.get("kind") == YOUTUBE_RSS_KIND:
        video_id = _as_text(payload.get("video_id"))
        direct = _as_text(payload.get("link"))
        if direct:
            return direct
        if video_id:
            return f"https://www.youtube.com/watch?v={video_id}"
    return _pick_text(payload, _URL_KEYS)


def _pick_text(payload: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        found = _as_text(payload.get(key))
        if found:
            return found
    return None


def _pick_published(payload: dict[str, Any]) -> datetime | None:
    for key in _PUBLISHED_KEYS:
        parsed = _coerce_datetime(payload.get(key))
        if parsed is not None:
            return parsed
    return None


def _as_text(value: Any) -> str | None:
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    if isinstance(value, dict):
        for key in ("content", "#text", "value", "text"):
            found = _as_text(value.get(key))
            if found:
                return found
        return None
    if isinstance(value, list):
        for item in value:
            found = _as_text(item)
            if found:
                return found
    return None


def _clean_url(value: Any) -> str | None:
    if not isinstance(value, str):
        return _as_text(value)
    stripped = value.strip()
    return stripped or None


def _coerce_datetime(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None) if value.tzinfo else value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(value, UTC).replace(tzinfo=None)
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=None) if parsed.tzinfo else parsed
    except ValueError:
        pass
    try:
        parsed = parsedate_to_datetime(text)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed.replace(tzinfo=None) if parsed.tzinfo else parsed
