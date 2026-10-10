"""CLI: sync each active shared source once, or use legacy per-user collection.

Usage (from backend/, PostgreSQL up, migrations applied, user + user_sources exist):

    uv run python -m app.services
    uv run python -m app.services --user demo@example.com
    uv run python -m app.services --user 1
    uv run python -m app.services --user demo@example.com --dry-run
    uv run python -m app.services --user demo@example.com --normalize
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from sqlalchemy.orm import Session

from app.connectors.base import InvalidSourceError
from app.connectors.youtube import DEFAULT_LIMIT
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.db.session import get_engine
from app.services.collect import UnknownUserError, UserCollectResult, collect_for_user
from app.services.source_sync import sync_active_sources


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Sync each active source once into shared feed_items. --user is legacy mode."
    )
    parser.add_argument(
        "--user",
        required=False,
        help="users.id or users.email",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help=f"Max items per source (YouTube default {DEFAULT_LIMIT})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and print without writing feed_items",
    )
    parser.add_argument(
        "--normalize",
        action="store_true",
        help="After persist, map collected raw_payload onto FeedItem fields",
    )
    args = parser.parse_args(argv)
    configure_logging(get_settings().log_level)
    logger = logging.getLogger("app.services")

    if args.limit is not None and args.limit < 1:
        print("limit must be >= 1", file=sys.stderr)
        return 2
    if args.user is None:
        if args.dry_run or args.normalize:
            parser.error("--dry-run and --normalize require --user")
        try:
            outcomes = asyncio.run(sync_active_sources(limit=args.limit))
        except Exception:
            logger.exception("Failed to list active sources")
            print("Failed to list active sources", file=sys.stderr)
            return 1
        for row in outcomes:
            print(
                f"source_id={row.source_id} type={row.source_type} status={row.status} "
                f"fetched={row.fetched} saved={row.saved} inserted={row.inserted} "
                f"updated={row.updated} reason={row.reason or ''}"
            )
        return 1 if any(row.status == "failed" for row in outcomes) else 0

    engine = get_engine()
    try:
        with Session(engine) as session:
            try:
                result = collect_for_user(
                    session,
                    args.user,
                    limit=args.limit or DEFAULT_LIMIT,
                    dry_run=args.dry_run,
                    normalize=args.normalize,
                )
            except UnknownUserError as exc:
                print(f"Unknown user: {exc}", file=sys.stderr)
                return 2
            except InvalidSourceError as exc:
                print(str(exc), file=sys.stderr)
                return 2
            if args.dry_run:
                session.rollback()
            else:
                session.commit()
    except Exception:
        logger.exception("Failed to collect sources for user")
        print("Failed to collect sources for the user", file=sys.stderr)
        return 1

    _print_result(result)
    return 1 if result.failed else 0


def _print_result(result: UserCollectResult) -> None:
    suffix = " dry-run" if result.dry_run else ""
    print(f"user_id={result.user_id} email={result.email}{suffix}")
    if not result.outcomes:
        print("sources=0")
    for row in result.outcomes:
        parts = [
            f"source_id={row.source_id}",
            f"type={row.source_type}",
            f"status={row.status}",
            f"fetched={row.fetched}",
            f"inserted={row.inserted}",
            f"updated={row.updated}",
        ]
        if row.normalized is not None:
            parts.append(f"normalized={row.normalized}")
        if row.reason:
            parts.append(f"reason={row.reason}")
        print(" ".join(parts))
    print(
        f"collected={result.collected} empty={result.empty} "
        f"skipped={result.skipped} failed={result.failed}"
    )


if __name__ == "__main__":
    sys.exit(main())
