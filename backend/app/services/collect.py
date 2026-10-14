"""Show one user's enabled sources from a shared source-centric sync run."""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.db.session import get_engine
from app.models.user import User
from app.models.user_source import UserSource
from app.services.source_sync import SourceSyncResult, sync_active_sources


class UnknownUserError(ValueError):
    """No users row matches the given id or email."""


@dataclass
class UserCollectResult:
    user_id: int
    email: str
    outcomes: list[SourceSyncResult] = field(default_factory=list)

    @property
    def collected(self) -> int:
        return sum(row.status == "collected" for row in self.outcomes)

    @property
    def empty(self) -> int:
        return sum(row.status == "empty" for row in self.outcomes)

    @property
    def skipped(self) -> int:
        return sum(row.status == "skipped" for row in self.outcomes)

    @property
    def failed(self) -> int:
        return sum(row.status == "failed" for row in self.outcomes)


def resolve_user(session: Session, identifier: str) -> User | None:
    key = identifier.strip()
    if not key:
        return None
    if key.isdigit():
        found = session.scalar(select(User).where(User.id == int(key)))
        if found is not None:
            return found
    return session.scalar(select(User).where(User.email == key))


async def collect_for_user(
    identifier: str,
    *,
    session_factory: sessionmaker[Session] | None = None,
    limit: int | None = None,
    synced_sources: list[SourceSyncResult] | None = None,
) -> UserCollectResult:
    """Project a shared sync run onto one user; never fetch per subscription.

    Pass ``synced_sources`` to reuse the same run for several users. Without it,
    all active sources are synchronized once, then only this user's enabled
    sources are returned. The production entry point remains sync_active_sources.
    """
    if limit is not None and limit < 1:
        raise ValueError("limit must be >= 1")
    factory = session_factory if session_factory is not None else sessionmaker(bind=get_engine())
    with factory() as session:
        user = resolve_user(session, identifier)
        if user is None:
            raise UnknownUserError(identifier.strip() or identifier)
        user_id, email = user.id, user.email
        source_ids = set(
            session.scalars(
                select(UserSource.source_id).where(
                    UserSource.user_id == user_id,
                    UserSource.enabled.is_(True),
                )
            ).all()
        )

    if not source_ids:
        return UserCollectResult(user_id=user_id, email=email)
    outcomes = (
        synced_sources
        if synced_sources is not None
        else await sync_active_sources(session_factory=factory, limit=limit)
    )
    return UserCollectResult(
        user_id=user_id,
        email=email,
        outcomes=[row for row in outcomes if row.source_id in source_ids],
    )
