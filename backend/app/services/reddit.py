import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.connectors.reddit.client import RedditClient
from app.connectors.reddit.errors import RedditSyncConflictError
from app.models import FeedItem, RedditSyncState, Source, SourceType
from app.processing.reddit import map_post


def normalize_subreddit(value: str) -> str:
    name = value.strip().strip("/").rsplit("/", 1)[-1]
    name = re.sub(r"^r/", "", name, flags=re.IGNORECASE).strip()
    if not re.fullmatch(r"[A-Za-z0-9_]{1,21}", name):
        raise ValueError("Укажите корректное имя subreddit")
    return name.lower()


@dataclass(frozen=True)
class SyncResult:
    subreddit: str
    fetched: int
    saved: int
    cursor_at: datetime | None


def _new_source(session: Session, subreddit: str) -> Source:
    url = f"https://www.reddit.com/r/{subreddit}/"
    stmt = (
        insert(Source)
        .values(
            type=SourceType.REDDIT, name=f"r/{subreddit}", canonical_url=url, external_id=subreddit
        )
        .on_conflict_do_update(
            index_elements=[Source.external_id],
            index_where=(Source.type == SourceType.REDDIT),
            set_={"name": f"r/{subreddit}", "canonical_url": url},
        )
        .returning(Source.id)
    )
    source_id = session.scalar(stmt)
    return session.get(Source, source_id)


def sync_reddit(
    session_factory,
    subreddit: str,
    *,
    limit: int | None = None,
    initial_limit: int = 50,
    overlap_seconds: int = 3600,
    client_factory=RedditClient,
    now: datetime | None = None,
) -> SyncResult:
    name = normalize_subreddit(subreddit)
    if (limit is not None and limit < 1) or initial_limit < 1 or overlap_seconds < 0:
        raise ValueError("Лимиты должны быть положительными, перекрытие — неотрицательным")
    run_started = (now or datetime.now(UTC)).astimezone(UTC).replace(tzinfo=None)

    # Короткая первая транзакция только считывает согласованный cursor; сеть выполняется отдельно.
    with session_factory() as session, session.begin():
        source = session.scalar(
            select(Source).where(Source.type == SourceType.REDDIT, Source.external_id == name)
        )
        state = (
            session.scalar(select(RedditSyncState).where(RedditSyncState.source_id == source.id))
            if source
            else None
        )
        expected_cursor = state.cursor_at if state else None
        initial_floor = state.initial_floor_at if state else None

    with client_factory() as client:
        if expected_cursor is None and initial_floor is None:
            posts = client.fetch_latest(name, limit=initial_limit)
            initial = True
        else:
            initial = False
            overlap = timedelta(seconds=overlap_seconds)
            lower = max(initial_floor - overlap, expected_cursor - overlap)
            posts = [
                post
                for post in client.fetch_range(name, lower, run_started)
                if post.created_utc >= initial_floor
            ]

    # Для явного инкрементального лимита считаются только новые external_id;
    # записи после границы остаются на следующем проходе.
    with session_factory() as session, session.begin():
        source_row = _new_source(session, name)
        state = session.scalar(
            select(RedditSyncState)
            .where(RedditSyncState.source_id == source_row.id)
            .with_for_update()
        )
        observed_cursor = state.cursor_at if state else None
        if observed_cursor != expected_cursor:
            raise RedditSyncConflictError("Курсор Reddit изменился; повторите запуск")
        if initial:
            selected = posts[:initial_limit]
            new_ids = {
                row[0]
                for row in session.execute(
                    select(FeedItem.external_id).where(FeedItem.source_id == source_row.id)
                )
            }
            rows = [post for post in selected if post.id not in new_ids]
        else:
            stored = {
                row[0]
                for row in session.execute(
                    select(FeedItem.external_id).where(FeedItem.source_id == source_row.id)
                )
            }
            unseen = [post for post in posts if post.id not in stored]
            rows = unseen[:limit] if limit is not None else unseen
        if rows:
            values = [{"source_id": source_row.id, **map_post(post)} for post in rows]
            session.execute(
                insert(FeedItem)
                .values(values)
                .on_conflict_do_nothing(index_elements=[FeedItem.source_id, FeedItem.external_id])
            )
        saved_boundary = max((p.created_utc for p in rows), default=expected_cursor)
        if initial and saved_boundary is None:
            # Фиксируем завершённый пустой initial scan и не повторяем его каждый запуск.
            saved_boundary = run_started
        new_floor = (
            min((p.created_utc for p in selected), default=run_started)
            if initial
            else initial_floor
        )
        if state is None:
            state = RedditSyncState(
                source_id=source_row.id,
                cursor_at=saved_boundary,
                initial_floor_at=new_floor,
                overlap_seconds=overlap_seconds,
            )
            session.add(state)
        else:
            if saved_boundary is not None:
                state.cursor_at = (
                    max(state.cursor_at, saved_boundary) if state.cursor_at else saved_boundary
                )
            state.initial_floor_at = new_floor
            state.overlap_seconds = overlap_seconds
            state.updated_at = run_started
        source_row.last_synced_at = run_started
        return SyncResult(name, len(posts), len(rows), saved_boundary)
