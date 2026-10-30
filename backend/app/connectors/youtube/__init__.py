"""YouTube channel connector.

Collects recent videos from a public channel RSS feed (feedparser, no API key)
and, when present, public captions via timedtext.

Run against one channel from backend/ after PostgreSQL is up and migrations applied:

    uv run python -m app.connectors.youtube "https://www.youtube.com/@GoogleDevelopers"

Print without writing to the database:

    uv run python -m app.connectors.youtube --dry-run "https://www.youtube.com/@GoogleDevelopers"
"""

from collections.abc import Callable

from sqlalchemy.orm import Session

from app.connectors.base import FetchResult
from app.connectors.youtube.parser import DEFAULT_LIMIT, YouTubeConnector
from app.connectors.youtube.persist import PersistStats, persist_fetch
from app.models.source import Source

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
    source: Source | None = None,
    session_factory: Callable[[], Session] | None = None,
) -> tuple[FetchResult, PersistStats]:
    """Прочитать ленту канала и записать сырые feed_items. Commit — у вызывающей стороны.

    ``source`` обновляет указанную строку каталога (user_sources) вместо поиска
    или создания другого YouTube-источника. ``session_factory`` читает кэш
    сохранённых расшифровок в отдельной короткой сессии: читающая транзакция
    закрывается до HTTP-запросов субтитров, поэтому пишущая транзакция ``session``
    не охватывает сетевые вызовы.
    """
    client = connector or YouTubeConnector()
    fetch = client.fetch(url, limit=limit, session=session, session_factory=session_factory)
    stats = persist_fetch(session, fetch, source=source)
    return fetch, stats
