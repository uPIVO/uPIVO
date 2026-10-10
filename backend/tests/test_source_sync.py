"""Source selection uses SQL DISTINCT, independent of the number of subscribers."""

import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.models.enums import SourceType
from app.services import source_sync
from app.services.source_sync import SourceSyncResult


@pytest.mark.parametrize(
    ("links", "expected"),
    [
        ([(1, 10, True), (2, 10, True)], [10]),
        ([(1, 10, True), (1, 20, True)], [10, 20]),
        ([(1, 10, False)], []),
        ([(1, 10, True), (2, 10, False)], [10]),
    ],
)
def test_active_sources_sync_once_per_distinct_enabled_source(monkeypatch, links, expected):
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE sources (id INTEGER PRIMARY KEY)"))
        connection.execute(
            text("CREATE TABLE user_sources (user_id INTEGER, source_id INTEGER, enabled BOOLEAN)")
        )
        for source_id in {link[1] for link in links}:
            connection.execute(text("INSERT INTO sources (id) VALUES (:id)"), {"id": source_id})
        for user_id, source_id, enabled in links:
            connection.execute(
                text("INSERT INTO user_sources VALUES (:user_id, :source_id, :enabled)"),
                {"user_id": user_id, "source_id": source_id, "enabled": enabled},
            )

    calls = []

    async def fake_sync_source(source_id, *, session_factory, limit):
        calls.append(source_id)
        return SourceSyncResult(source_id, "youtube", "empty")

    monkeypatch.setattr(source_sync, "sync_source", fake_sync_source)
    results = asyncio.run(
        source_sync.sync_active_sources(session_factory=sessionmaker(bind=engine))
    )
    assert calls == expected
    assert [row.source_id for row in results] == expected
    engine.dispose()


class _SourceSession:
    def __init__(self, source):
        self.source = source
        self.commits = 0

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def get(self, _model, source_id):
        return self.source if self.source.id == source_id else None

    def commit(self):
        self.commits += 1


def test_sync_source_delegates_to_youtube_service(monkeypatch):
    source = SimpleNamespace(
        id=10,
        type=SourceType.YOUTUBE,
        canonical_url="https://youtube.test/channel",
        external_id="c",
    )
    sessions = []

    def factory():
        session = _SourceSession(source)
        sessions.append(session)
        return session

    calls = []

    def fake_collect(session, url, *, limit, source):
        calls.append((session, url, limit, source))
        return SimpleNamespace(items=[object()]), SimpleNamespace(inserted=1, updated=0)

    monkeypatch.setattr(source_sync, "collect_channel", fake_collect)
    result = asyncio.run(source_sync.sync_source(10, session_factory=factory, limit=3))
    assert len(calls) == 1
    assert calls[0][2:] == (3, source)
    assert sessions[-1].commits == 1
    assert (result.fetched, result.saved, result.inserted, result.updated) == (1, 1, 1, 0)


def test_sync_source_delegates_to_reddit_service(monkeypatch):
    source = SimpleNamespace(
        id=20, type=SourceType.REDDIT, canonical_url="https://reddit.test/r/python",
        external_id="python",
    )
    calls = []

    def fake_reddit(factory, subreddit, *, limit):
        calls.append((factory, subreddit, limit))
        return SimpleNamespace(fetched=4, saved=2)

    monkeypatch.setattr(source_sync, "sync_reddit", fake_reddit)
    def factory():
        return _SourceSession(source)
    result = asyncio.run(source_sync.sync_source(20, session_factory=factory))
    assert calls == [(factory, "python", None)]
    assert (result.fetched, result.saved, result.status) == (4, 2, "collected")
