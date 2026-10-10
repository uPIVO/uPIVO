"""CLI: normalize pending feed_items (or a JSON fixture) into the shared FeedItem shape.

Usage (from backend/):

    uv run python -m app.processing --fixture tests/fixtures/normalize/sample.json
    uv run python -m app.processing
    uv run python -m app.processing --dry-run --limit 10
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.db.session import get_engine
from app.models.enums import ProcessingStatus
from app.processing.normalize import list_pending_feed_items, map_raw_payload, normalize_feed_item


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Map raw feed_item payloads to title, body_text, original_url, author, "
            "published_at and content_hash. raw_payload is left unchanged."
        )
    )
    parser.add_argument(
        "--fixture",
        type=Path,
        help="JSON file with one payload object or a list of payloads (no database)",
    )
    parser.add_argument(
        "--item-id",
        type=int,
        help="Normalize a single pending feed_items.id",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Max pending rows (or fixture entries) to process",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print mapped fields without writing processing_status",
    )
    args = parser.parse_args(argv)
    configure_logging(get_settings().log_level)

    if args.limit is not None and args.limit < 1:
        print("limit must be >= 1", file=sys.stderr)
        return 2
    if args.fixture is not None and args.item_id is not None:
        print("--fixture and --item-id cannot be used together", file=sys.stderr)
        return 2

    if args.fixture is not None:
        return _run_fixture(args.fixture, limit=args.limit)

    return _run_database(item_id=args.item_id, limit=args.limit, dry_run=args.dry_run)


def _run_fixture(path: Path, *, limit: int | None) -> int:
    try:
        payloads = _load_fixture(path)
    except FileNotFoundError:
        print(f"Fixture not found: {path}", file=sys.stderr)
        return 2
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"Invalid fixture: {exc}", file=sys.stderr)
        return 2

    if limit is not None:
        payloads = payloads[:limit]

    processed = 0
    failed = 0
    for payload in payloads:
        outcome = map_raw_payload(_unwrap_payload(payload))
        if not outcome.ok or outcome.content is None:
            failed += 1
            print(f"failed\t{outcome.reason or 'invalid payload'}")
            continue
        mapped = outcome.content
        processed += 1
        print(
            f"processed\t{mapped.content_hash}\t{mapped.title or ''}\t"
            f"{mapped.original_url}\t{mapped.author or ''}"
        )
    print(f"processed={processed} failed={failed}")
    return 0


def _run_database(*, item_id: int | None, limit: int | None, dry_run: bool) -> int:
    logger = logging.getLogger("app.processing")
    engine = get_engine()
    processed = 0
    failed = 0
    skipped = 0
    try:
        with Session(engine) as session:
            items = list_pending_feed_items(session, limit=limit, item_id=item_id)
            for item in items:
                if item.processing_status != ProcessingStatus.PENDING:
                    skipped += 1
                    print(f"{item.id}\tskipped\t{item.processing_status}")
                    continue
                if dry_run:
                    outcome = map_raw_payload(item.raw_payload, fallback=item)
                else:
                    outcome = normalize_feed_item(item)
                if not outcome.ok or outcome.content is None:
                    failed += 1
                    print(f"{item.id}\tfailed\t{outcome.reason or 'invalid payload'}")
                    continue
                mapped = outcome.content
                processed += 1
                print(
                    f"{item.id}\tnormalized\t{mapped.content_hash}\t"
                    f"{mapped.title or ''}\t{mapped.original_url}"
                )
            if dry_run:
                session.rollback()
            else:
                session.commit()
    except Exception:
        logger.exception("Failed to normalize feed_items")
        print("Failed to normalize feed_items in the database", file=sys.stderr)
        return 1

    print(f"processed={processed} failed={failed} skipped={skipped}{' dry-run' if dry_run else ''}")
    return 0


def _load_fixture(path: Path) -> list[Any]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict):
        return [raw]
    raise ValueError("fixture must be a JSON object or a list of objects")


def _unwrap_payload(record: Any) -> Any:
    if isinstance(record, dict) and isinstance(record.get("raw_payload"), dict):
        return record["raw_payload"]
    return record


if __name__ == "__main__":
    sys.exit(main())
