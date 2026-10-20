"""Source selection uses SQL DISTINCT, independent of the number of subscribers."""

import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.connectors.habr.client import HabrClient
from app.models.enums import SourceType
from app.models.source import Source
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


def test_sync_sources_deduplicates_explicit_ids(monkeypatch):
    calls = []

    async def fake_sync_source(source_id, *, session_factory, limit):
        calls.append((source_id, session_factory, limit))
        return SourceSyncResult(source_id, "youtube", "empty")

    monkeypatch.setattr(source_sync, "sync_source", fake_sync_source)
    factory = object()
    results = asyncio.run(
        source_sync.sync_sources([20, 10, 20, 10], session_factory=factory, limit=3)
    )
    assert calls == [(10, factory, 3), (20, factory, 3)]
    assert [row.source_id for row in results] == [10, 20]


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


def test_sync_source_delegates_to_habr_service(monkeypatch):
    source = SimpleNamespace(
        id=30, type=SourceType.HABR, canonical_url=HabrClient.RSS_URL,
        external_id="habr:articles",
    )
    sessions = []
    calls = []

    def factory():
        session = _SourceSession(source)
        sessions.append(session)
        return session

    class FakeHabrService:
        def __init__(self, session):
            calls.append(session)

        async def sync_publications(self, limit=None):
            calls.append(limit)
            return SimpleNamespace(fetched=5, saved=2)

    monkeypatch.setattr(source_sync, "HabrService", FakeHabrService)
    result = asyncio.run(source_sync.sync_source(30, session_factory=factory, limit=3))
    assert calls == [sessions[-1], 3]
    assert result == SourceSyncResult(
        source_id=30, source_type="habr", status="collected",
        fetched=5, saved=2, inserted=2, updated=0,
    )


def test_sync_source_delegates_to_telegram_service(monkeypatch):
    url = "https://t.me/example_channel"
    source = SimpleNamespace(
        id=40, type=SourceType.TELEGRAM, canonical_url=url, external_id="-10012345",
    )
    channel = object()
    calls = []

    def factory():
        return _SourceSession(source)

    class FakeTelegramConnector:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def resolve_channel(self, given_url):
            calls.append(("resolve", given_url))
            return channel

    async def fake_sync(given_channel, connector, given_factory, *, limit):
        calls.append(("sync", given_channel, connector, given_factory, limit))
        return 3, 99

    monkeypatch.setattr(source_sync, "TelegramConnector", FakeTelegramConnector)
    monkeypatch.setattr(source_sync, "map_channel", lambda _: {"external_id": "-10012345"})
    monkeypatch.setattr(source_sync, "sync_telegram_channel", fake_sync)
    result = asyncio.run(source_sync.sync_source(40, session_factory=factory, limit=7))
    assert calls[0] == ("resolve", url)
    assert calls[1][0] == "sync"
    assert calls[1][1] is channel
    assert isinstance(calls[1][2], FakeTelegramConnector)
    assert calls[1][3:] == (factory, 7)
    assert result == SourceSyncResult(
        source_id=40, source_type="telegram", status="collected", fetched=3, saved=3,
    )


def test_telegram_source_without_external_id_keeps_user_link_source(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE sources (id INTEGER PRIMARY KEY, type TEXT NOT NULL, "
                "name TEXT NOT NULL, canonical_url TEXT NOT NULL, external_id TEXT, "
                "last_synced_at DATETIME, created_at DATETIME)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO sources (id, type, name, canonical_url) VALUES "
                "(40, 'telegram', 'Pending channel', 'https://t.me/example_channel')"
            )
        )
    factory = sessionmaker(bind=engine)
    channel = object()

    class FakeTelegramConnector:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def resolve_channel(self, url):
            assert url == "https://t.me/example_channel"
            return channel

    async def fake_sync(given_channel, connector, given_factory, *, limit):
        assert given_channel is channel and given_factory is factory and limit is None
        with factory() as session:
            assert session.get(Source, 40).external_id == "-10012345"
        return 2, 7

    monkeypatch.setattr(source_sync, "TelegramConnector", FakeTelegramConnector)
    monkeypatch.setattr(
        source_sync,
        "map_channel",
        lambda _: {
            "external_id": "-10012345",
            "canonical_url": "https://t.me/example_channel",
            "name": "Example channel",
        },
    )
    monkeypatch.setattr(source_sync, "sync_telegram_channel", fake_sync)

    result = asyncio.run(source_sync.sync_source(40, session_factory=factory))
    assert result.status == "collected" and result.source_id == 40
    with factory() as session:
        source = session.get(Source, 40)
        assert source.external_id == "-10012345" and source.name == "Example channel"
    engine.dispose()
