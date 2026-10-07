from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy.sql.dml import Insert

from app.connectors.reddit.client import RedditClient
from app.connectors.reddit.errors import RedditSaturatedWindowError, RedditSyncConflictError
from app.connectors.reddit.models import RedditPost
from app.processing.reddit import map_post
from app.services.reddit import sync_reddit


def payload(post_id: int, second: int) -> dict:
    return {
        "id": str(post_id),
        "title": f"post {post_id}",
        "selftext": "",
        "author": "user",
        "permalink": f"/r/test/comments/{post_id}/",
        "created_utc": second,
    }


class Response:
    def __init__(self, data):
        self.data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self.data


class FakeHTTP:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.params = []

    def get(self, url, *, params, timeout):
        self.params.append(params)
        return Response(next(self.responses))


def test_mapper_uses_title_for_empty_selftext_and_naive_utc():
    post = RedditPost.from_payload(payload(1, 1700000000))
    mapped = map_post(post)
    assert mapped["body_text"] == post.title
    assert mapped["external_id"] == "1"
    assert mapped["original_url"] == "https://www.reddit.com/r/test/comments/1/"
    assert post.created_utc == datetime.fromtimestamp(1700000000, UTC).replace(tzinfo=None)


def test_fetch_range_deduplicates_inclusive_split_boundary():
    rows = [payload(i, 1704067200 + i) for i in range(99)] + [payload(99, 1704110400)]
    # Один и тот же результат на границе дочерних окон приходит из обеих сторон.
    http = FakeHTTP([rows, [rows[-1]], [rows[-1]]])
    client = RedditClient(http_client=http)
    result = client.fetch_range("test", datetime(2024, 1, 1), datetime(2024, 1, 2))
    assert len(result) == 1
    assert {post.id for post in result} == {"99"}
    assert all(isinstance(params["after"], str) for params in http.params)
    client.close()


def test_fetch_latest_supports_limits_over_one_hundred():
    newest = [payload(i, 1704067300 + i) for i in range(100)]
    older = [payload(i + 100, 1704067299) for i in range(50)]
    http = FakeHTTP([newest, older])
    client = RedditClient(http_client=http)
    result = client.fetch_latest("test", 150)
    assert len(result) == 150
    assert len({post.id for post in result}) == 150
    client.close()


def test_saturated_one_second_window_fails_closed():
    http = FakeHTTP([[payload(i, 1704067200) for i in range(100)]])
    client = RedditClient(http_client=http)
    with pytest.raises(RedditSaturatedWindowError):
        client.fetch_range("test", datetime(2024, 1, 1), datetime(2024, 1, 1, 0, 0, 1))
    client.close()


def test_fetch_latest_defaults_to_fifty():
    rows = [payload(i, 1704067200 + i) for i in range(50)]
    http = FakeHTTP([rows])
    client = RedditClient(http_client=http)
    result = client.fetch_latest("test")
    assert len(result) == 50
    assert http.params[0]["limit"] == 50
    client.close()


class FakeSession:
    def __init__(self, scalar_results, *, fail_insert=False):
        self.scalar_results = iter(scalar_results)
        self.fail_insert = fail_insert
        self.added = []
        self.committed = False
        self.rolled_back = False
        self.inserts = []

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
            self.inserts.append(statement)
            if self.fail_insert:
                raise RuntimeError("ошибка записи")
        return []

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

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def fetch_latest(self, subreddit, limit):
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


def test_cursor_race_aborts_before_ingestion_commit():
    current = datetime(2024, 1, 2)
    read = FakeSession([None])
    write = FakeSession([1, SimpleNamespace(cursor_at=current)])
    factory = FakeSessionFactory([read, write])
    with pytest.raises(RedditSyncConflictError):
        sync_reddit(factory, "test", client_factory=lambda: FakeSyncClient([]))
    assert write.rolled_back
    assert not write.inserts


def test_feed_write_failure_rolls_back_state_update():
    post = RedditPost.from_payload(payload(1, 1700000000))
    read = FakeSession([None])
    write = FakeSession([1, None], fail_insert=True)
    factory = FakeSessionFactory([read, write])
    with pytest.raises(RuntimeError, match="ошибка записи"):
        sync_reddit(factory, "test", client_factory=lambda: FakeSyncClient([post]))
    assert write.rolled_back
    assert not write.added
