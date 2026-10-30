"""CLI: collect recent videos from one public YouTube channel.

Usage (from backend/, with PostgreSQL and migrations):

    uv run python -m app.connectors.youtube "https://www.youtube.com/@GoogleDevelopers"
    uv run python -m app.connectors.youtube --dry-run "https://www.youtube.com/channel/UC_x5XG1OV2P6uZZ5FSM9Ttw"
"""

from __future__ import annotations

import argparse
import logging
import sys

from sqlalchemy.orm import Session, sessionmaker

from app.connectors.base import ConnectorError, InvalidSourceError, SourceFetchError
from app.connectors.youtube.parser import DEFAULT_LIMIT, YouTubeConnector
from app.connectors.youtube.persist import persist_fetch
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.db.session import get_engine


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Collect recent videos from a public YouTube channel RSS feed."
    )
    parser.add_argument(
        "url",
        help="Public channel URL, e.g. https://www.youtube.com/@GoogleDevelopers",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_LIMIT,
        help=f"Max videos to store (default {DEFAULT_LIMIT}, YouTube RSS typically has up to 15)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and print videos without writing to the database",
    )
    args = parser.parse_args(argv)
    configure_logging(get_settings().log_level)
    logger = logging.getLogger("app.connectors.youtube")

    if args.dry_run:
        try:
            fetch = YouTubeConnector().fetch(args.url, limit=args.limit)
        except InvalidSourceError as exc:
            print(f"Invalid YouTube channel: {exc}", file=sys.stderr)
            return 2
        except SourceFetchError as exc:
            print(f"Failed to read YouTube: {exc}", file=sys.stderr)
            return 1
        except ConnectorError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        _print_fetch(fetch)
        return 0

    engine = get_engine()
    try:
        with Session(engine) as session:
            try:
                fetch = YouTubeConnector().fetch(
                    args.url,
                    limit=args.limit,
                    session_factory=sessionmaker(bind=engine),
                )
            except InvalidSourceError as exc:
                print(f"Invalid YouTube channel: {exc}", file=sys.stderr)
                return 2
            except SourceFetchError as exc:
                print(f"Failed to read YouTube: {exc}", file=sys.stderr)
                return 1
            except ConnectorError as exc:
                print(str(exc), file=sys.stderr)
                return 1
            stats = persist_fetch(session, fetch)
            session.commit()
    except Exception:
        logger.exception("Failed to store YouTube videos")
        print("Failed to store YouTube videos in the database", file=sys.stderr)
        return 1

    print(
        f"source_id={stats.source_id} channel={fetch.source_external_id} "
        f"name={fetch.source_name!r} fetched={len(fetch.items)} "
        f"inserted={stats.inserted} updated={stats.updated}"
    )
    for item in fetch.items:
        published = item.published_at.isoformat() if item.published_at else ""
        captions = "yes" if item.raw_payload.get("transcript") else "no"
        print(
            f"{item.external_id}\t{published}\t{item.original_url}\t"
            f"{item.title or ''}\tcaptions={captions}"
        )
    return 0


def _print_fetch(fetch) -> None:
    print(
        f"dry-run channel={fetch.source_external_id} "
        f"name={fetch.source_name!r} fetched={len(fetch.items)}"
    )
    for item in fetch.items:
        published = item.published_at.isoformat() if item.published_at else ""
        captions = "yes" if item.raw_payload.get("transcript") else "no"
        print(
            f"{item.external_id}\t{published}\t{item.original_url}\t"
            f"{item.title or ''}\tcaptions={captions}"
        )


if __name__ == "__main__":
    sys.exit(main())
