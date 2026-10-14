"""User subscriptions project one shared sync run; collectors are always mocked."""

import asyncio

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.services import source_sync
from app.services.__main__ import main
from app.services.collect import UnknownUserError, collect_for_user
from app.services.source_sync import SourceSyncResult


@pytest.fixture
def subscriptions():
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT, password_hash TEXT, "
                "created_at DATETIME, updated_at DATETIME)"
            )
        )
        connection.execute(text("CREATE TABLE sources (id INTEGER PRIMARY KEY)"))
        connection.execute(
            text("CREATE TABLE user_sources (user_id INTEGER, source_id INTEGER, enabled BOOLEAN)")
        )
        connection.execute(
            text("INSERT INTO users (id, email, password_hash) VALUES "
                 "(1, 'one@example.com', 'x'), (2, 'two@example.com', 'x')")
        )
        for source_id in (10, 20, 30, 40, 50, 60):
            connection.execute(text("INSERT INTO sources VALUES (:id)"), {"id": source_id})
        for user_id, source_id, enabled in (
            (1, 10, True),  # YouTube: shared by both users
            (1, 20, True),  # Telegram
            (1, 30, True),  # Reddit
            (1, 40, True),  # Habr
            (1, 50, False),  # disabled for user 1, enabled for user 2
            (1, 60, False),  # disabled for everyone
            (2, 10, True),
            (2, 50, True),
            (2, 60, False),
        ):
            connection.execute(
                text("INSERT INTO user_sources VALUES (:user_id, :source_id, :enabled)"),
                {"user_id": user_id, "source_id": source_id, "enabled": enabled},
            )
    yield sessionmaker(bind=engine)
    engine.dispose()


def _fake_dispatch(monkeypatch):
    types = {10: "youtube", 20: "telegram", 30: "reddit", 40: "habr", 50: "reddit"}
    calls = []

    async def fake_sync_source(source_id, *, session_factory, limit):
        calls.append(source_id)
        return SourceSyncResult(source_id, types[source_id], "collected", fetched=1, saved=1)

    monkeypatch.setattr(source_sync, "sync_source", fake_sync_source)
    return calls


def test_user_sees_all_four_types_from_one_shared_run(monkeypatch, subscriptions):
    calls = _fake_dispatch(monkeypatch)
    result = asyncio.run(
        collect_for_user("one@example.com", session_factory=subscriptions)
    )
    assert calls == [10, 20, 30, 40, 50]
    assert [(row.source_id, row.source_type) for row in result.outcomes] == [
        (10, "youtube"), (20, "telegram"), (30, "reddit"), (40, "habr")
    ]
    assert result.collected == 4
    assert result.user_id == 1


def test_two_users_share_one_fetch_and_keep_distinct_enabled_sources(monkeypatch, subscriptions):
    calls = _fake_dispatch(monkeypatch)
    shared = asyncio.run(source_sync.sync_active_sources(session_factory=subscriptions))
    first = asyncio.run(
        collect_for_user("1", session_factory=subscriptions, synced_sources=shared)
    )
    second = asyncio.run(
        collect_for_user("two@example.com", session_factory=subscriptions, synced_sources=shared)
    )
    assert calls.count(10) == 1
    assert calls == [10, 20, 30, 40, 50]
    assert [row.source_id for row in first.outcomes] == [10, 20, 30, 40]
    assert [row.source_id for row in second.outcomes] == [10, 50]
    assert 60 not in calls


def test_unknown_user_does_not_fetch(monkeypatch, subscriptions):
    calls = _fake_dispatch(monkeypatch)
    with pytest.raises(UnknownUserError):
        asyncio.run(collect_for_user("missing@example.com", session_factory=subscriptions))
    assert calls == []


def test_user_with_only_disabled_sources_does_not_fetch(monkeypatch, subscriptions):
    calls = _fake_dispatch(monkeypatch)
    with subscriptions() as session:
        session.execute(text("UPDATE user_sources SET enabled = 0 WHERE user_id = 1"))
        session.commit()
    result = asyncio.run(collect_for_user("1", session_factory=subscriptions))
    assert result.outcomes == []
    assert calls == []


def test_cli_user_prints_shared_results(monkeypatch, capsys):
    async def fake_collect(identifier, *, limit):
        assert (identifier, limit) == ("one@example.com", None)
        from app.services.collect import UserCollectResult

        return UserCollectResult(
            user_id=1,
            email=identifier,
            outcomes=[SourceSyncResult(20, "telegram", "collected", fetched=2, saved=2)],
        )

    monkeypatch.setattr("app.services.__main__.collect_for_user", fake_collect)
    assert main(["--user", "one@example.com"]) == 0
    output = capsys.readouterr().out
    assert "type=telegram status=collected fetched=2 saved=2" in output


def test_cli_unknown_user_exits_2(monkeypatch, capsys):
    async def fake_collect(identifier, *, limit):
        raise UnknownUserError(identifier)

    monkeypatch.setattr("app.services.__main__.collect_for_user", fake_collect)
    assert main(["--user", "missing@example.com"]) == 2
    assert "Unknown user" in capsys.readouterr().err


def test_cli_default_runs_source_centric_sync(monkeypatch):
    async def fake_sync_active_sources(*, limit):
        assert limit is None
        return []

    monkeypatch.setattr("app.services.__main__.sync_active_sources", fake_sync_active_sources)
    assert main([]) == 0


def test_cli_rejects_non_positive_limit():
    assert main(["--user", "1", "--limit", "0"]) == 2
