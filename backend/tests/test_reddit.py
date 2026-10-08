import sys
import time
from datetime import UTC, datetime, timedelta, timezone
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
from app.connectors.reddit.models import RedditFetchBatch, RedditPost
from app.processing.reddit import map_post
from app.services.reddit import sync_reddit
from scripts import reddit_sync
from scripts.collect_reddit import main as collect_reddit_main


def payload(post_id: int | str, second: int, **overrides) -> dict:
    return {
        "id": str(post_id),
        "title": f"post {post_id}",
        "selftext": "",
        "author": "user",
        "permalink": f"/r/test/comments/{post_id}/",
        "created_utc": second,
        **overrides,
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


@pytest.mark.parametrize(
    ("fields", "excluded"),
    [
        ({"title": " [DeLeTeD] "}, True),
        ({"selftext": " [REMOVED]\n"}, True),
        ({"removed_by_category": "automod_filtered"}, True),
        ({"selftext": "", "url": "https://example.com/article"}, False),
        ({"author": "[deleted]"}, False),
        ({"removed_by_category": "  "}, False),
    ],
)
def test_archival_removal_predicate_uses_only_explicit_post_evidence(fields, excluded):
    post = RedditPost.from_payload(payload(1, 1700000000, **fields))
    assert post.is_archived_removed is excluded


@pytest.mark.parametrize("inclusive", [True, False])
def test_fetch_range_covers_split_boundaries_with_either_api_inclusivity(inclusive):
    start = datetime(2024, 1, 1)
    midpoint = datetime(2024, 1, 1, 12)
    rows = [payload("at-start", int(start.replace(tzinfo=UTC).timestamp()))]
    rows += [payload(i, 1704067200 + i + 1) for i in range(100)]
    rows.append(payload("at-midpoint", int(midpoint.replace(tzinfo=UTC).timestamp())))
    requests = []

    def handler(request):
        params = dict(request.url.params)
        requests.append(params)
        after = datetime.fromisoformat(params["after"]).replace(tzinfo=None)
        before = datetime.fromisoformat(params["before"]).replace(tzinfo=None)

        def matches(row):
            created = datetime.fromtimestamp(row["created_utc"], UTC).replace(tzinfo=None)
            return after <= created <= before if inclusive else after < created < before

        matched = [row for row in rows if matches(row)]
        return response(sorted(matched, key=lambda row: row["created_utc"])[:100])

    client = make_client(handler)
    result = client.fetch_range("test", start, datetime(2024, 1, 2))
    ids = {post.id for post in result}
    assert {str(i) for i in range(100)} | {"at-midpoint"} <= ids
    assert ("at-start" in ids) is inclusive
    assert 2 < len(requests) < client.max_requests
    close_client(client)


def test_search_serializes_dates_as_outward_rounded_utc_milliseconds():
    offset = timezone(timedelta(hours=5))
    after = datetime(2024, 1, 1, 5, 0, 0, 123456, tzinfo=offset)
    before = datetime(2024, 1, 1, 5, 0, 0, 999999, tzinfo=offset)
    requests = []
    client = make_client(
        lambda request: (requests.append(dict(request.url.params)), response([]))[1]
    )
    client.fetch_range("test", after, before)
    assert requests == [
        {
            "subreddit": "test",
            "sort": "asc",
            "limit": "100",
            "after": "2024-01-01T00:00:00.123Z",
            "before": "2024-01-01T00:00:01.000Z",
        }
    ]
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
    batch = client.fetch_latest("test", 150)
    assert len(batch.posts) == 150
    assert len({post.id for post in batch.posts}) == 150
    assert {f"tie-{i}" for i in range(20)} <= {post.id for post in batch.posts}
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
    batch = client.fetch_latest("test", 250)
    assert len(batch.posts) == 100
    close_client(client)


def test_saturated_one_second_window_fails_closed():
    client = make_client(lambda _: response([payload(i, 1704067200) for i in range(100)]))
    with pytest.raises(RedditSaturatedWindowError):
        client.fetch_range("test", datetime(2024, 1, 1), datetime(2024, 1, 1, 0, 0, 1))
    close_client(client)


def test_fetch_latest_defaults_to_fifty_and_accepts_documented_data_envelope():
    rows = [payload(i, 1704067200 + i) for i in range(50)]
    client = make_client(lambda _: response(rows))
    batch = client.fetch_latest("test")
    assert len(batch.posts) == 50
    close_client(client)


def test_fetch_latest_refills_removed_rows_and_keeps_inspected_high_water():
    hidden = [payload(i, 1704067300 + i, removed_by_category="moderator") for i in range(98)]
    page = hidden + [payload("keep-1", 1704067299), payload("keep-2", 1704067298)]
    older = [payload(f"old-{i}", 1704067297) for i in range(3)]

    def handler(request):
        params = dict(request.url.params)
        if "after" not in params:
            return response(page)
        after = datetime.fromisoformat(params["after"]).timestamp()
        before = datetime.fromisoformat(params["before"]).timestamp()
        return response([row for row in older if after <= row["created_utc"] <= before])

    client = make_client(handler)
    batch = client.fetch_latest("test", 5)
    assert {post.id for post in batch.posts} == {"keep-1", "keep-2", "old-0", "old-1", "old-2"}
    assert batch.scanned_through == datetime.fromtimestamp(1704067397, UTC).replace(tzinfo=None)
    assert batch.inspected_count == 103
    close_client(client)


def test_fetch_latest_as_of_excludes_future_rows_and_high_water():
    as_of = datetime(2024, 1, 1)
    rows = [payload("future", 1893456000), payload("valid", 1704067200)]
    requests = []

    def handler(request):
        requests.append(dict(request.url.params))
        return response(rows)

    client = make_client(handler)
    batch = client.fetch_latest("test", 1, as_of=as_of)
    assert [post.id for post in batch.posts] == ["valid"]
    assert batch.scanned_through == datetime(2024, 1, 1)
    assert requests[0]["before"] == "2024-01-01T00:00:00.000Z"
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


def test_http_422_exposes_only_safe_normalized_validation_detail():
    client = make_client(
        lambda _: httpx.Response(
            422,
            json={"detail": [{"msg": "invalid subreddit\nsecret=abc", "input": "do-not-show"}]},
        )
    )
    with pytest.raises(RedditRequestError) as raised:
        client.fetch_latest("test", 5)
    message = str(raised.value)
    assert "HTTP 422" in message
    assert "invalid subreddit" in message
    assert "do-not-show" not in message
    assert "abc" not in message
    assert "https://" not in message
    assert "subreddit=python" not in message
    assert "\n" not in message
    close_client(client)


def test_oversized_http_error_body_stops_read_and_keeps_status():
    reads = []

    class OversizedStream(httpx.SyncByteStream):
        def __iter__(self):
            reads.append(1)
            yield b"x" * 100
            reads.append(2)
            yield b"unread"

    client = make_client(
        lambda _: httpx.Response(422, stream=OversizedStream()), max_response_bytes=16
    )
    with pytest.raises(RedditRequestError) as raised:
        client.fetch_latest("test", 5)
    assert "HTTP 422" in str(raised.value)
    assert "размер" in str(raised.value)
    assert reads == [1]
    close_client(client)


def test_http_error_body_deadline_keeps_status():
    def handler(_):
        time.sleep(0.02)
        return httpx.Response(422, json={"detail": "validation failed"})

    client = make_client(handler, deadline_seconds=0.005)
    with pytest.raises(RedditRequestError) as raised:
        client.fetch_latest("test", 5)
    assert "HTTP 422" in str(raised.value)
    assert "срок чтения" in str(raised.value)
    close_client(client)


def test_malformed_http_error_body_is_sanitized_and_truncated():
    body = b"not-json\x00 password=abc Authorization: Bearer private-token " + b"x" * 600
    client = make_client(lambda _: httpx.Response(422, content=body), max_response_bytes=1024)
    with pytest.raises(RedditRequestError) as raised:
        client.fetch_latest("test", 5)
    message = str(raised.value)
    assert "HTTP 422" in message
    assert "abc" not in message
    assert "private-token" not in message
    assert "\x00" not in message
    assert len(message) < 400
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
    def __init__(self, posts, scanned_through=None):
        self.posts = posts
        self.scanned_through = scanned_through or max(
            (post.created_utc for post in posts), default=None
        )
        self.initial_limit = None
        self.range_bounds = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def fetch_latest(self, subreddit, limit, *, as_of=None):
        self.initial_limit = limit
        return RedditFetchBatch(self.posts, self.scanned_through, len(self.posts))

    def fetch_range(self, subreddit, after, before):
        self.range_bounds.append((after, before))
        return self.posts


class FakePreviewClient:
    def __init__(self, posts):
        self.posts = posts

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def fetch_latest(self, subreddit, limit):
        return RedditFetchBatch(
            self.posts,
            max((post.created_utc for post in self.posts), default=None),
            len(self.posts),
        )


def preview_post(post_id=1, *, selftext="полный текст", **fields):
    data = payload(
        post_id,
        1704067200,
        title="Заголовок",
        selftext=selftext,
        permalink=f"/r/test/comments/{post_id}/",
    )
    data.update(fields)
    return RedditPost.from_payload(data)


def test_preview_default_output_remains_compact(monkeypatch, capsys):
    post = preview_post()
    monkeypatch.setattr(sys, "argv", ["reddit_sync", "python"])
    monkeypatch.setattr(reddit_sync, "RedditClient", lambda: FakePreviewClient([post]))
    assert reddit_sync.main() == 0
    assert capsys.readouterr().out == (
        "[2024-01-01T00:00:00] Заголовок\nhttps://www.reddit.com/r/test/comments/1/\n\n"
    )


def test_preview_full_prints_complete_multiline_selftext(monkeypatch, capsys):
    text = "первая строка\n" + "а" * 400 + "\nпоследняя строка"
    post = preview_post(selftext=text)
    monkeypatch.setattr(sys, "argv", ["reddit_sync", "python", "--full"])
    monkeypatch.setattr(reddit_sync, "RedditClient", lambda: FakePreviewClient([post]))
    assert reddit_sync.main() == 0
    assert capsys.readouterr().out == (
        "[2024-01-01T00:00:00] Заголовок\n"
        "https://www.reddit.com/r/test/comments/1/\n" + text + "\n\n"
    )


@pytest.mark.parametrize("full", [False, True])
def test_preview_omits_archive_removed_posts(monkeypatch, capsys, full):
    posts = [
        preview_post(1, title="[deleted]"),
        preview_post(2, selftext="[removed]"),
        preview_post(3, removed_by_category="automod_filtered"),
    ]
    argv = ["reddit_sync", "python"] + (["--full"] if full else [])
    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.setattr(reddit_sync, "RedditClient", lambda: FakePreviewClient(posts))
    assert reddit_sync.main() == 0
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize(
    ("selftext", "notice"),
    [
        ("", "Текст поста отсутствует в ответе Arctic Shift."),
        ("  \n ", "Текст поста отсутствует в ответе Arctic Shift."),
    ],
)
def test_preview_full_reports_empty_content(monkeypatch, capsys, selftext, notice):
    post = preview_post(selftext=selftext)
    monkeypatch.setattr(sys, "argv", ["reddit_sync", "python", "--full"])
    monkeypatch.setattr(reddit_sync, "RedditClient", lambda: FakePreviewClient([post]))
    assert reddit_sync.main() == 0
    assert capsys.readouterr().out == (
        f"[2024-01-01T00:00:00] Заголовок\nhttps://www.reddit.com/r/test/comments/1/\n{notice}\n\n"
    )


def test_collect_cli_rejects_negative_overlap(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["collect_reddit", "python", "--overlap-seconds", "-1"])
    with pytest.raises(SystemExit) as raised:
        collect_reddit_main()
    assert raised.value.code == 2
    assert "--overlap-seconds не может быть отрицательным" in capsys.readouterr().err


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
    assert write.added[0].cursor_at <= datetime(2024, 1, 1)
    assert [row["external_id"] for batch in write.insert_values for row in batch] == ["1"]


def test_initial_cursor_uses_inspected_high_water_but_floor_uses_oldest_eligible():
    eligible = RedditPost.from_payload(payload(1, 1704067200))
    latest_hidden = datetime(2024, 1, 1, 0, 1)
    client = FakeSyncClient([eligible], scanned_through=latest_hidden)
    read = FakeSession([None])
    write = FakeSession([1, None], execute_results=[[]])
    sync_reddit(
        FakeSessionFactory([read, write]),
        "test",
        client_factory=lambda: client,
        now=datetime(2024, 1, 2, tzinfo=UTC),
    )
    assert write.added[0].cursor_at == latest_hidden
    assert write.added[0].initial_floor_at == eligible.created_utc


def test_incremental_all_filtered_posts_advance_cursor_without_saving():
    cursor = datetime(2024, 1, 1)
    latest_hidden = datetime(2024, 1, 1, 0, 0, 20)
    state = SimpleNamespace(cursor_at=cursor, initial_floor_at=cursor, overlap_seconds=0)
    source = SimpleNamespace(id=1)
    hidden = [
        RedditPost.from_payload(payload("removed", 1704067210, selftext="[REMOVED]")),
        RedditPost.from_payload(payload("moderated", 1704067220, removed_by_category="reddit")),
    ]
    read, write = (
        FakeSession([source, state]),
        FakeSession([1, state], execute_results=[[]]),
    )
    result = sync_reddit(
        FakeSessionFactory([read, write]),
        "test",
        limit=1,
        client_factory=lambda: FakeSyncClient(hidden),
        now=datetime(2024, 1, 2, tzinfo=UTC),
    )
    assert result.fetched == result.saved == 0
    assert state.cursor_at == latest_hidden
    assert write.insert_values == []


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
            RedditPost.from_payload(
                payload("removed", int(tied.replace(tzinfo=UTC).timestamp()), selftext="[removed]")
            ),
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
            RedditPost.from_payload(
                payload("removed", int(tied.replace(tzinfo=UTC).timestamp()), selftext="[removed]")
            ),
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
        def fetch_latest(self, subreddit, limit, *, as_of=None):
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
