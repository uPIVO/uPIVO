"""CLI: glue feed_items into content_clusters.

Usage (from backend/):

    uv run python -m app.clustering --fixture tests/fixtures/cluster/sample.json
    uv run python -m app.clustering
    uv run python -m app.clustering --dry-run
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.clustering.cluster import (
    StoryItem,
    build_plans,
    cluster_views,
    format_report,
    unclustered_count,
)
from app.clustering.persist import cluster_feed
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.db.session import get_engine


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Glue feed items into stories. Same content_hash is one story. "
            "Similar titles and bodies share a content_clusters row. "
            "Publications are not deleted."
        )
    )
    parser.add_argument(
        "--fixture",
        type=Path,
        help="JSON list of items (id, title, body_text, optional content_hash and published_at)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print clusters without writing content_clusters",
    )
    args = parser.parse_args(argv)
    configure_logging(get_settings().log_level)

    if args.fixture is not None:
        return _run_fixture(args.fixture, dry_run=args.dry_run)
    return _run_database(dry_run=args.dry_run)


def _run_fixture(path: Path, *, dry_run: bool) -> int:
    try:
        items = _load_fixture(path)
        plans = build_plans(items)
    except FileNotFoundError:
        print(f"Fixture not found: {path}", file=sys.stderr)
        return 2
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"Invalid fixture: {exc}", file=sys.stderr)
        return 2

    views = cluster_views(plans, items)
    print(format_report(views, unclustered=unclustered_count(plans, items), dry_run=dry_run))
    return 0


def _run_database(*, dry_run: bool) -> int:
    logger = logging.getLogger("app.clustering")
    engine = get_engine()
    try:
        with Session(engine) as session:
            views, unclustered = cluster_feed(session, write=not dry_run)
            if dry_run:
                session.rollback()
            else:
                session.commit()
    except Exception:
        logger.exception("Failed to cluster feed_items")
        print("Failed to cluster feed_items in the database", file=sys.stderr)
        return 1
    print(format_report(views, unclustered=unclustered, dry_run=dry_run))
    return 0


def _load_fixture(path: Path) -> list[StoryItem]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, dict):
        records = [raw]
    elif isinstance(raw, list):
        records = raw
    else:
        raise ValueError("fixture must be a JSON object or a list of objects")
    items: list[StoryItem] = []
    for record in records:
        items.append(_story_from_record(record))
    return items


def _story_from_record(record: Any) -> StoryItem:
    if not isinstance(record, dict):
        raise ValueError("fixture entry must be an object")
    if "id" not in record or isinstance(record["id"], bool) or not isinstance(record["id"], int):
        raise ValueError("fixture entry needs integer id")
    title = record.get("title")
    body = record.get("body_text", "")
    if title is not None and not isinstance(title, str):
        raise ValueError(f"title must be a string for id {record['id']}")
    if not isinstance(body, str):
        raise ValueError(f"body_text must be a string for id {record['id']}")
    content_hash = record.get("content_hash")
    if content_hash is not None and not isinstance(content_hash, str):
        raise ValueError(f"content_hash must be a string for id {record['id']}")
    return StoryItem(
        id=record["id"],
        title=title,
        body_text=body,
        content_hash=content_hash,
        published_at=_parse_published(record.get("published_at"), record["id"]),
    )


def _parse_published(value: Any, item_id: int) -> datetime | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ValueError(f"published_at must be an ISO string for id {item_id}")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"published_at is not ISO time for id {item_id}") from exc
    return parsed.replace(tzinfo=None) if parsed.tzinfo else parsed


if __name__ == "__main__":
    sys.exit(main())
