"""FeedItem normalization: raw_payload → unified fields.

After connectors dump raw rows, this step fills title, body_text, original_url,
author, published_at and content_hash. raw_payload is left as collected.
processing_status moves pending → normalized / failed.

Connectors may call `normalize_feed_item` after persist. CLI from backend/:

    uv run python -m app.processing --fixture tests/fixtures/normalize/sample.json

Pending rows in PostgreSQL (migrations applied):

    uv run python -m app.processing
"""

from app.processing.normalize import (
    NormalizedContent,
    NormalizeOutcome,
    NormalizeStats,
    list_pending_feed_items,
    map_raw_payload,
    normalize_feed_item,
    normalize_pending,
)

__all__ = [
    "NormalizedContent",
    "NormalizeOutcome",
    "NormalizeStats",
    "list_pending_feed_items",
    "map_raw_payload",
    "normalize_feed_item",
    "normalize_pending",
]
