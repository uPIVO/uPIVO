"""Collect shared feed_items for one user's enabled sources.

sources stay global. user_sources says which of those THIS user turned on.
500 users on one channel still means one source row and one fetch per run;
items land in shared feed_items, not a per-user copy.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.connectors.base import ConnectorError, FetchResult, InvalidSourceError, SourceFetchError
from app.connectors.youtube import DEFAULT_LIMIT, YouTubeConnector, collect_channel
from app.models.enums import SourceType
from app.models.feed_item import FeedItem
from app.models.source import Source
from app.models.user import User
from app.models.user_source import UserSource
from app.processing.normalize import normalize_feed_item


class UnknownUserError(ValueError):
    """No users row matches the given id or email."""


@dataclass(frozen=True)
class SourceCollectOutcome:
    source_id: int
    source_type: str
    canonical_url: str
    status: str
    fetched: int = 0
    inserted: int = 0
    updated: int = 0
    normalized: int | None = None
    reason: str | None = None


@dataclass
class UserCollectResult:
    user_id: int
    email: str
    outcomes: list[SourceCollectOutcome] = field(default_factory=list)
    dry_run: bool = False

    @property
    def collected(self) -> int:
        return sum(1 for row in self.outcomes if row.status == "collected")

    @property
    def empty(self) -> int:
        return sum(1 for row in self.outcomes if row.status == "empty")

    @property
    def skipped(self) -> int:
        return sum(1 for row in self.outcomes if row.status == "skipped")

    @property
    def failed(self) -> int:
        return sum(1 for row in self.outcomes if row.status == "failed")


def resolve_user(session: Session, identifier: str) -> User | None:
    key = identifier.strip()
    if not key:
        return None
    if key.isdigit():
        found = session.scalar(select(User).where(User.id == int(key)))
        if found is not None:
            return found
    return session.scalar(select(User).where(User.email == key))


def list_user_source_rows(session: Session, user_id: int) -> list[tuple[UserSource, Source]]:
    stmt = (
        select(UserSource, Source)
        .join(Source, UserSource.source_id == Source.id)
        .where(UserSource.user_id == user_id)
        .order_by(UserSource.id)
    )
    return [(row[0], row[1]) for row in session.execute(stmt).all()]


def normalize_collected(session: Session, source_id: int, fetch: FetchResult) -> int:
    """Post-persist hook: map just-collected rows. Does not change the YouTube fetcher."""
    items = _collected_items(session, source_id, [item.external_id for item in fetch.items])
    count = 0
    for item in items:
        outcome = normalize_feed_item(item)
        if outcome.ok:
            count += 1
    return count


def collect_for_user(
    session: Session,
    identifier: str,
    *,
    limit: int = DEFAULT_LIMIT,
    dry_run: bool = False,
    normalize: bool = False,
    youtube: YouTubeConnector | None = None,
) -> UserCollectResult:
    """Fetch enabled sources for one user. Caller commits. YouTube uses collect_channel."""
    if limit < 1:
        raise InvalidSourceError("limit must be >= 1")

    user = resolve_user(session, identifier)
    if user is None:
        raise UnknownUserError(identifier.strip() or identifier)

    result = UserCollectResult(user_id=user.id, email=user.email, dry_run=dry_run)
    client = youtube or YouTubeConnector()
    for link, source in list_user_source_rows(session, user.id):
        result.outcomes.append(
            _collect_one(
                session,
                link,
                source,
                client=client,
                limit=limit,
                dry_run=dry_run,
                normalize=normalize,
            )
        )
    return result


def _collect_one(
    session: Session,
    link: UserSource,
    source: Source,
    *,
    client: YouTubeConnector,
    limit: int,
    dry_run: bool,
    normalize: bool,
) -> SourceCollectOutcome:
    if not link.enabled:
        return _outcome(source, status="skipped", reason="disabled")
    if source.type is not SourceType.YOUTUBE:
        return _outcome(
            source,
            status="skipped",
            reason=f"unsupported:{source.type.value}",
        )

    try:
        if dry_run:
            fetch = client.fetch(source.canonical_url, limit=limit)
            inserted = 0
            updated = 0
            source_id = source.id
        else:
            fetch, stats = collect_channel(
                session,
                source.canonical_url,
                limit=limit,
                connector=client,
                source=source,
            )
            inserted = stats.inserted
            updated = stats.updated
            source_id = stats.source_id
    except (InvalidSourceError, SourceFetchError, ConnectorError) as exc:
        return _outcome(source, status="failed", reason=str(exc))

    normalized: int | None = None
    if normalize and not dry_run:
        normalized = normalize_collected(session, source_id, fetch)

    status = "empty" if not fetch.items else "collected"
    return _outcome(
        source,
        status=status,
        fetched=len(fetch.items),
        inserted=inserted,
        updated=updated,
        normalized=normalized,
        source_id=source_id,
    )


def _collected_items(session: Session, source_id: int, external_ids: list[str]) -> list[FeedItem]:
    if not external_ids:
        return []
    return list(
        session.scalars(
            select(FeedItem).where(
                FeedItem.source_id == source_id,
                FeedItem.external_id.in_(external_ids),
            )
        ).all()
    )


def _outcome(
    source: Source,
    *,
    status: str,
    fetched: int = 0,
    inserted: int = 0,
    updated: int = 0,
    normalized: int | None = None,
    reason: str | None = None,
    source_id: int | None = None,
) -> SourceCollectOutcome:
    return SourceCollectOutcome(
        source_id=source_id if source_id is not None else source.id,
        source_type=source.type.value,
        canonical_url=source.canonical_url,
        status=status,
        fetched=fetched,
        inserted=inserted,
        updated=updated,
        normalized=normalized,
        reason=reason,
    )
