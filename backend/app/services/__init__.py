"""Product scenarios. User-scoped collection lives here.

sources stay global; user_sources selects which ones THIS user enabled.
Collect once per source into shared feed_items.

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

__all__ = [
    "SourceCollectOutcome",
    "UnknownUserError",
    "UserCollectResult",
    "collect_for_user",
    "list_user_source_rows",
    "normalize_collected",
    "resolve_user",
]
