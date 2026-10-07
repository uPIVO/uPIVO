import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy.sql.dml import Insert

from app.connectors.reddit.client import RedditClient
from app.connectors.reddit.errors import (
    RedditRequestError,
    RedditSaturatedWindowError,
    RedditSyncConflictError,
)
from app.connectors.reddit.models import RedditPost
from app.processing.reddit import map_post
from app.services.reddit import sync_reddit


def payload(post_id: int | str, second: int) -> dict:
    return {
        "id": str(post_id),
        "title": f"post {post_id}",
        "selftext": "",
        "author": "user",
        "permalink": f"/r/test/comments/{post_id}/",
        "created_utc": second,
    }


def make_client(handler, **kwargs):
    transport = httpx.MockTransport(handler)
    return RedditClient(http_client=httpx.Client(transport=transport), **kwargs)


def response(rows):
    return httpx.Response(200, json={"data": rows})


def close_client(client):
    client.close()
    client.http_client.close()


def test_mapper_uses_title_for_empty_selftext_and_naive_utc():
    post = RedditPost.from_payload(payload(1, 1700000000))
    mapped = map_post(post)
    assert mapped["body_text"] == post.title
    assert mapped["external_id"] == "1"
    assert mapped["original_url"] == "https://www.reddit.com/r/test/comments/1/"
    assert post.created_utc == datetime.fromtimestamp(1700000000, UTC).replace(tzinfo=None)


def test_fetch_range_covers_all_rows_and_deduplicates_inclusive_split_boundary():
    midpoint = datetime(2024, 1, 1, 12)
    rows = [payload(i, 1704067200 + i) for i in range(99)] + [
        payload(99, int(midpoint.replace(tzinfo=UTC).timestamp()))
    ]
    requests = []

    def handler(request):
        params = dict(request.url.params)
        requests.append(params)
        if "after" not in params:
            return response(rows)
        after = datetime.fromisoformat(params["after"]).replace(tzinfo=None)
        before = datetime.fromisoformat(params["before"]).replace(tzinfo=None)
        matched = [
            row
            for row in rows
            if after
            <= datetime.fromtimestamp(row["created_utc"], UTC).replace(tzinfo=None)
            <= before
        ]
        return response(sorted(matched, key=lambda row: row["created_utc"])[:100])

    client = make_client(handler)
    result = client.fetch_range("test", datetime(2024, 1, 1), datetime(2024, 1, 2))
    assert len(result) == 100
    assert {post.id for post in result} == {str(i) for i in range(100)}
    assert len(requests) > 2
    close_client(client)


def test_fetch_latest_supports_limit_over_one_hundred_and_collects_cutoff_ties():
    cutoff = 1704067300
    first_page = [payload(i, cutoff + 1 + i) for i in range(90)]
    first_page += [payload(f"tie-{i}", cutoff) for i in range(10)]
    remaining = [payload(f"tie-{i}", cutoff) for i in range(10, 20)]
    remaining += [payload(f"older-{i}", cutoff - 1) for i in range(40)]
    requests = []

    def handler(request):
        params = dict(request.url.params)
        requests.append(params)
        if "after" not in params:
            return response(first_page)
        after = datetime.fromisoformat(params["after"]).timestamp()
        before = datetime.fromisoformat(params["before"]).timestamp()
        matched = [row for row in remaining if after <= row["created_utc"] <= before]
        return response(sorted(matched, key=lambda row: row["created_utc"])[:100])

    client = make_client(handler)
    posts = client.fetch_latest("test", 150)
    assert len(posts) == 150
    assert len({post.id for post in posts}) == 150
    assert {f"tie-{i}" for i in range(20)} <= {post.id for post in posts}
    assert len(requests) >= 2
    close_client(client)


def test_fetch_latest_returns_available_history_without_spinning_past_2005():
    start = int(datetime(2005, 1, 1, tzinfo=UTC).timestamp())
    rows = [payload(i, start + 365 * 86400 + i * 100) for i in range(100)]

    def handler(request):
        params = dict(request.url.params)
        if "after" not in params:
            return response(sorted(rows, key=lambda row: row["created_utc"], reverse=True)[:100])
        after = datetime.fromisoformat(params["after"]).timestamp()
        before = datetime.fromisoformat(params["before"]).timestamp()
        matched = [row for row in rows if after <= row["created_utc"] <= before]
        return response(sorted(matched, key=lambda row: row["created_utc"])[:100])

    client = make_client(handler)
    posts = client.fetch_latest("test", 250)
    assert len(posts) == 100
    close_client(client)


def test_saturated_one_second_window_fails_closed():
    client = make_client(lambda _: response([payload(i, 1704067200) for i in range(100)]))
    with pytest.raises(RedditSaturatedWindowError):
        client.fetch_range("test", datetime(2024, 1, 1), datetime(2024, 1, 1, 0, 0, 1))
    close_client(client)


def test_fetch_latest_defaults_to_fifty_and_accepts_documented_data_envelope():
    rows = [payload(i, 1704067200 + i) for i in range(50)]
    client = make_client(lambda _: response(rows))
    posts = client.fetch_latest("test")
    assert len(posts) == 50
    close_client(client)


@pytest.mark.parametrize(
    "body", [{"items": []}, {"data": {}}, {"data": [None]}, {"data": [{"id": "missing-time"}]}]
)
def test_malformed_envelopes_fail_with_reddit_error(body):
    client = make_client(lambda _: httpx.Response(200, json=body))
    with pytest.raises(RedditRequestError):
        client.fetch_latest("test", 5)
    close_client(client)


def test_streamed_response_size_is_bounded_without_content_length():
    class OversizedStream(httpx.SyncByteStream):
        def __iter__(self):
            yield b'{"data":'
            yield b"[]}"

    def handler(_):
        return httpx.Response(200, stream=OversizedStream())

    client = make_client(handler, max_response_bytes=8)
    with pytest.raises(RedditRequestError, match="размер"):
        client.fetch_latest("test", 5)
    close_client(client)


def test_deadline_is_checked_after_stream_read():
    def handler(_):
        time.sleep(0.02)
        return response([])

    client = make_client(handler, deadline_seconds=0.005)
    with pytest.raises(RedditRequestError, match="срок"):
        client.fetch_latest("test", 5)
    close_client(client)


class FakeSession:
    def __init__(self, scalar_results, *, execute_results=None, fail_insert=False):
        self.scalar_results = iter(scalar_results)
        self.execute_results = iter(execute_results or [])
        self.fail_insert = fail_insert
        self.added = []
        self.committed = False
        self.rolled_back = False
        self.insert_values = []

    def __enter__(self):
        return self

    def begin(self):
        return self

    def __exit__(self, exc_type, *_):
        if exc_type:
            self.rolled_back = True
        else:
            self.committed = True
        return False

    def scalar(self, statement):
        return next(self.scalar_results)

    def get(self, model, source_id):
        return SimpleNamespace(id=source_id, last_synced_at=None)

    def execute(self, statement):
        if isinstance(statement, Insert):
            self.insert_values.append(
                [
                    {key.key: value for key, value in row.items()}
                    for row in statement._multi_values[0]
                ]
            )
            if self.fail_insert:
                raise RuntimeError("ошибка записи")
            return []
        return next(self.execute_results, [])

    def add(self, value):
        self.added.append(value)


class FakeSessionFactory:
    def __init__(self, sessions):
        self.sessions = iter(sessions)
        self.created = []

    def __call__(self):
        session = next(self.sessions)
        self.created.append(session)
        return session


class FakeSyncClient:
    def __init__(self, posts):
        self.posts = posts
        self.initial_limit = None
        self.range_bounds = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def fetch_latest(self, subreddit, limit):
        self.initial_limit = limit
        return self.posts

    def fetch_range(self, subreddit, after, before):
        self.range_bounds.append((after, before))
        return self.posts


def test_empty_initial_scan_persists_initialized_cursor():
    read = FakeSession([None])
    write = FakeSession([1, None])
    factory = FakeSessionFactory([read, write])
    result = sync_reddit(
        factory,
        "Test",
        client_factory=lambda: FakeSyncClient([]),
        now=datetime(2024, 1, 1, tzinfo=UTC),
    )
    assert result.saved == 0
    assert write.added[0].cursor_at == datetime(2024, 1, 1)
    assert write.committed


def test_first_run_limit_overrides_initial_limit_and_discards_future_posts():
    post = RedditPost.from_payload(payload(1, 1700000000))
    future = RedditPost.from_payload(payload(2, 1893456000))
    client = FakeSyncClient([future, post])
    read = FakeSession([None])
    write = FakeSession([1, None], execute_results=[[]])
    result = sync_reddit(
        FakeSessionFactory([read, write]),
        "test",
        limit=7,
        client_factory=lambda: client,
        now=datetime(2024, 1, 1, tzinfo=UTC),
    )
    assert client.initial_limit == 7
    assert result.saved == 1
    assert [row["external_id"] for batch in write.insert_values for row in batch] == ["1"]


def test_incremental_limit_resumes_equal_time_ids_with_boundary_replay():
    cursor = datetime(2024, 1, 1)
    tied = datetime(2024, 1, 1, 0, 0, 10)
    state = SimpleNamespace(cursor_at=cursor, initial_floor_at=cursor, overlap_seconds=0)
    source = SimpleNamespace(id=1)
    read1, write1 = (
        FakeSession([source, state]),
        FakeSession([1, state], execute_results=[[("old",)]]),
    )
    client1 = FakeSyncClient(
        [
            RedditPost.from_payload(payload("a", int(tied.replace(tzinfo=UTC).timestamp()))),
            RedditPost.from_payload(payload("b", int(tied.replace(tzinfo=UTC).timestamp()))),
            RedditPost.from_payload(payload("c", int(tied.replace(tzinfo=UTC).timestamp()) + 1)),
        ]
    )
    sync_reddit(
        FakeSessionFactory([read1, write1]),
        "test",
        limit=1,
        overlap_seconds=0,
        client_factory=lambda: client1,
        now=datetime(2024, 1, 2, tzinfo=UTC),
    )
    assert [row["external_id"] for batch in write1.insert_values for row in batch] == ["a"]
    assert state.cursor_at == tied
    assert client1.range_bounds[0][0] == cursor - timedelta(seconds=1)

    read2, write2 = (
        FakeSession([source, state]),
        FakeSession([1, state], execute_results=[[("old",), ("a",)]]),
    )
    client2 = FakeSyncClient(
        [
            RedditPost.from_payload(payload("a", int(tied.replace(tzinfo=UTC).timestamp()))),
            RedditPost.from_payload(payload("b", int(tied.replace(tzinfo=UTC).timestamp()))),
            RedditPost.from_payload(payload("c", int(tied.replace(tzinfo=UTC).timestamp()) + 1)),
        ]
    )
    sync_reddit(
        FakeSessionFactory([read2, write2]),
        "test",
        limit=1,
        overlap_seconds=0,
        client_factory=lambda: client2,
        now=datetime(2024, 1, 2, tzinfo=UTC),
    )
    assert [row["external_id"] for batch in write2.insert_values for row in batch] == ["b"]
    assert state.cursor_at == tied


def test_late_row_does_not_move_cursor_backward():
    cursor = datetime(2024, 1, 2)
    floor = datetime(2024, 1, 1)
    late = RedditPost.from_payload(
        payload("late", int(datetime(2024, 1, 1, 12, tzinfo=UTC).timestamp()))
    )
    state = SimpleNamespace(cursor_at=cursor, initial_floor_at=floor, overlap_seconds=0)
    source = SimpleNamespace(id=1)
    read, write = FakeSession([source, state]), FakeSession([1, state], execute_results=[[]])
    sync_reddit(
        FakeSessionFactory([read, write]),
        "test",
        overlap_seconds=0,
        client_factory=lambda: FakeSyncClient([late]),
        now=datetime(2024, 1, 3, tzinfo=UTC),
    )
    assert state.cursor_at == cursor
    assert [row["external_id"] for batch in write.insert_values for row in batch] == ["late"]


def test_api_failure_happens_before_any_write_transaction():
    class BrokenClient(FakeSyncClient):
        def fetch_latest(self, subreddit, limit):
            raise RedditRequestError("сбой API")

    read = FakeSession([None])
    factory = FakeSessionFactory([read])
    with pytest.raises(RedditRequestError):
        sync_reddit(factory, "test", client_factory=lambda: BrokenClient([]))
    assert factory.created == [read]
    assert not read.added
    assert not read.insert_values


def test_cursor_race_aborts_before_ingestion_commit():
    current = datetime(2024, 1, 2)
    read = FakeSession([None])
    write = FakeSession([1, SimpleNamespace(cursor_at=current)])
    factory = FakeSessionFactory([read, write])
    with pytest.raises(RedditSyncConflictError):
        sync_reddit(factory, "test", client_factory=lambda: FakeSyncClient([]))
    assert write.rolled_back
    assert not write.insert_values


def test_feed_write_failure_rolls_back_state_update():
    post = RedditPost.from_payload(payload(1, 1700000000))
    read = FakeSession([None])
    write = FakeSession([1, None], execute_results=[[]], fail_insert=True)
    factory = FakeSessionFactory([read, write])
    with pytest.raises(RuntimeError, match="ошибка записи"):
        sync_reddit(factory, "test", client_factory=lambda: FakeSyncClient([post]))
    assert write.rolled_back
    assert not write.added
