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
    effective_initial_limit = limit if limit is not None else initial_limit
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
        expected_cursor: datetime | None = state.cursor_at if state else None
        initial_floor: datetime | None = state.initial_floor_at if state else None

    inspected_high_water: datetime | None = None
    with client_factory() as client:
        if expected_cursor is None and initial_floor is None:
            batch = client.fetch_latest(name, limit=effective_initial_limit, as_of=run_started)
            inspected_high_water = (
                min(batch.scanned_through, run_started)
                if batch.scanned_through is not None
                else None
            )
            posts = [
                post
                for post in batch.posts
                if post.created_utc <= run_started and not post.is_archived_removed
            ]
            initial = True
        else:
            if expected_cursor is None or initial_floor is None:
                raise RedditSyncConflictError("Состояние синхронизации Reddit неполно")
            initial = False
            # Отдельная секундная репликация нужна даже при отключённом позднем overlap.
            replay = timedelta(seconds=max(1, overlap_seconds))
            lower = max(initial_floor - replay, expected_cursor - replay)
            inspected = [
                post
                for post in client.fetch_range(name, lower, run_started)
                if post.created_utc >= initial_floor and post.created_utc <= run_started
            ]
            inspected_high_water = max((post.created_utc for post in inspected), default=None)
            posts = [post for post in inspected if not post.is_archived_removed]

    # Для явного инкрементального лимита считаются только новые eligible external_id.
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
        fetched_count = len(posts)
        if initial:
            selected = posts[:effective_initial_limit]
            new_floor = min((post.created_utc for post in selected), default=run_started)
            new_ids = {
                row[0]
                for row in session.execute(
                    select(FeedItem.external_id).where(FeedItem.source_id == source_row.id)
                )
            }
            rows = [post for post in selected if post.id not in new_ids]
            if selected:
                initial_timestamps = [post.created_utc for post in selected]
                if inspected_high_water is not None:
                    initial_timestamps.append(inspected_high_water)
                saved_boundary = max(initial_timestamps)
            else:
                # Завершённый пустой initial scan не запускает повторное чтение истории.
                new_floor = run_started
                saved_boundary = run_started
        else:
            assert expected_cursor is not None
            cursor_before_write = expected_cursor
            new_floor = initial_floor
            stored = {
                row[0]
                for row in session.execute(
                    select(FeedItem.external_id).where(FeedItem.source_id == source_row.id)
                )
            }
            unseen = [post for post in posts if post.id not in stored]
            rows = unseen[:limit] if limit is not None else unseen
            if limit is not None and len(unseen) > limit:
                saved_boundary = max(
                    cursor_before_write,
                    max(post.created_utc for post in rows),
                )
            else:
                saved_boundary = max(
                    cursor_before_write,
                    inspected_high_water
                    if inspected_high_water is not None
                    else cursor_before_write,
                )
        if rows:
            values = [{"source_id": source_row.id, **map_post(post)} for post in rows]
            session.execute(
                insert(FeedItem)
                .values(values)
                .on_conflict_do_nothing(index_elements=[FeedItem.source_id, FeedItem.external_id])
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
            state.cursor_at = saved_boundary
            state.initial_floor_at = new_floor
            state.overlap_seconds = overlap_seconds
            state.updated_at = run_started
        source_row.last_synced_at = run_started
        return SyncResult(name, fetched_count, len(rows), saved_boundary)
