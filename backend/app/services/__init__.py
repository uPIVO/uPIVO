"""Product scenarios. Shared source synchronization lives here.

sources stay global; user_sources selects which ones THIS user enabled.
Collect once per active source into shared feed_items.

Run from backend/ after PostgreSQL is up and migrations applied:

    uv run python -m app.services --user demo@example.com
"""

from app.services.collect import (
    SourceCollectOutcome,
    UnknownUserError,
    UserCollectResult,
    collect_for_user,
    list_user_source_rows,
    normalize_collected,
    resolve_user,
)
from app.services.source_sync import SourceSyncResult, sync_active_sources, sync_source

__all__ = [
    "SourceCollectOutcome",
    "SourceSyncResult",
    "UnknownUserError",
    "UserCollectResult",
    "collect_for_user",
    "list_user_source_rows",
    "normalize_collected",
    "resolve_user",
    "sync_active_sources",
    "sync_source",
]
