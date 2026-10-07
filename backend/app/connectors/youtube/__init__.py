"""YouTube channel connector.

Collects recent videos from a public channel RSS feed (feedparser, no API key)
and, when present, public captions via timedtext.

Run against one channel from backend/ after PostgreSQL is up and migrations applied:

    uv run python -m app.connectors.youtube "https://www.youtube.com/@GoogleDevelopers"

Print without writing to the database:

    uv run python -m app.connectors.youtube --dry-run "https://www.youtube.com/@GoogleDevelopers"
"""

from sqlalchemy.orm import Session

from app.connectors.base import FetchResult
from app.connectors.youtube.parser import DEFAULT_LIMIT, YouTubeConnector
from app.connectors.youtube.persist import PersistStats, persist_fetch

__all__ = [
    "DEFAULT_LIMIT",
    "PersistStats",
    "YouTubeConnector",
    "collect_channel",
    "persist_fetch",
]


def collect_channel(
    session: Session,
    url: str,
    *,
    limit: int = DEFAULT_LIMIT,
    connector: YouTubeConnector | None = None,
) -> tuple[FetchResult, PersistStats]:
    """Fetch a channel RSS feed and upsert raw feed_items. Caller commits."""
    client = connector or YouTubeConnector()
    fetch = client.fetch(url, limit=limit, session=session)
    stats = persist_fetch(session, fetch)
    return fetch, stats
