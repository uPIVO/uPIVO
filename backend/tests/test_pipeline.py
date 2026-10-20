"""Publication pipeline chains existing stages. Collectors stay faked."""

import asyncio
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.pipeline.__main__ import main
from app.pipeline.run import chain_payloads, run_pipeline
from app.processing.normalize import NormalizeStats
from app.services import source_sync
from app.services.collect import UnknownUserError
from app.services.source_sync import SourceSyncResult

FIXTURE = Path(__file__).parent / "fixtures" / "pipeline" / "sample.json"


@pytest.fixture
def users():
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT, password_hash TEXT, "
                "created_at DATETIME, updated_at DATETIME)"
            )
        )
        connection.execute(text("CREATE TABLE sources (id INTEGER PRIMARY KEY, type TEXT)"))
        connection.execute(
            text("CREATE TABLE user_sources (user_id INTEGER, source_id INTEGER, enabled BOOLEAN)")
        )
        connection.execute(
            text(
                "INSERT INTO users (id, email, password_hash) VALUES "
                "(1, 'one@example.com', 'x'), (2, 'empty@example.com', 'x')"
            )
        )
        connection.execute(text("INSERT INTO sources (id, type) VALUES (10, 'youtube')"))
        connection.execute(
            text("INSERT INTO user_sources (user_id, source_id, enabled) VALUES (1, 10, 1)")
        )
    yield sessionmaker(bind=engine)
    engine.dispose()


def _patch_stages(monkeypatch, *, pending: int = 2):
    order: list[str] = []
    writes: list[bool] = []
    state = {"pending": pending}

    def fake_normalize(session, *, limit=None, item_id=None, now=None):
        order.append("normalize")
        processed = state["pending"]
        state["pending"] = 0
        return NormalizeStats(processed=processed, failed=0, skipped=0)

    def fake_cluster(session, *, write):
        order.append("cluster")
        writes.append(write)
        return [], 1

    def fake_extract(session):
        order.append("extract")
        return 0

    monkeypatch.setattr("app.pipeline.run.normalize_pending", fake_normalize)
    monkeypatch.setattr("app.pipeline.run._extract_entities", fake_extract)
    monkeypatch.setattr("app.pipeline.run.cluster_feed", fake_cluster)
    return order, writes


def test_empty_user_skips_collect_then_normalizes_and_clusters(monkeypatch, users):
    order, writes = _patch_stages(monkeypatch, pending=0)
    calls: list[int] = []

    async def fake_sync_source(source_id, *, session_factory, limit):
        calls.append(source_id)
        return SourceSyncResult(source_id, "youtube", "collected")

    monkeypatch.setattr(source_sync, "sync_source", fake_sync_source)
    result = asyncio.run(run_pipeline("empty@example.com", session_factory=users))
    assert calls == []
    assert result.collect.outcomes == []
    assert order == ["normalize", "extract", "cluster"]
    assert writes == [True]
    assert result.normalize.processed == 0


def test_youtube_source_collects_then_normalizes_then_clusters(monkeypatch, users):
    order, writes = _patch_stages(monkeypatch)
    calls: list[int] = []

    async def fake_sync_source(source_id, *, session_factory, limit):
        calls.append(source_id)
        order.append("collect")
        assert limit == 3
        return SourceSyncResult(
            source_id, "youtube", "collected", fetched=2, saved=2, inserted=2, updated=0
        )

    monkeypatch.setattr(source_sync, "sync_source", fake_sync_source)
    result = asyncio.run(run_pipeline("1", session_factory=users, limit=3))
    assert calls == [10]
    assert order == ["collect", "normalize", "extract", "cluster"]
    assert writes == [True]
    assert result.collect.outcomes[0].source_type == "youtube"
    assert result.collect.collected == 1
    assert result.normalize.processed == 2


def test_rerun_calls_the_same_stages_and_second_normalize_is_idle(monkeypatch, users):
    order, writes = _patch_stages(monkeypatch, pending=2)

    async def fake_sync_source(source_id, *, session_factory, limit):
        order.append("collect")
        return SourceSyncResult(
            source_id, "youtube", "collected", fetched=2, saved=2, inserted=0, updated=2
        )

    monkeypatch.setattr(source_sync, "sync_source", fake_sync_source)
    first = asyncio.run(run_pipeline("one@example.com", session_factory=users))
    second = asyncio.run(run_pipeline("one@example.com", session_factory=users))
    assert order == [
        "collect", "normalize", "extract", "cluster",
        "collect", "normalize", "extract", "cluster",
    ]
    assert writes == [True, True]
    assert first.normalize.processed == 2
    assert second.normalize.processed == 0
    assert second.collect.outcomes[0].updated == 2


def test_dry_run_does_not_collect_or_commit(monkeypatch, users):
    order, writes = _patch_stages(monkeypatch)
    events: list[str] = []
    calls: list[int] = []

    async def fake_sync_source(source_id, *, session_factory, limit):
        calls.append(source_id)
        return SourceSyncResult(source_id, "youtube", "collected")

    monkeypatch.setattr(source_sync, "sync_source", fake_sync_source)

    def factory():
        session = users()
        commit = session.commit
        rollback = session.rollback

        def tracked_commit():
            events.append("commit")
            return commit()

        def tracked_rollback():
            events.append("rollback")
            return rollback()

        session.commit = tracked_commit
        session.rollback = tracked_rollback
        return session

    result = asyncio.run(run_pipeline("one@example.com", session_factory=factory, dry_run=True))
    assert calls == []
    assert order == ["normalize", "extract", "cluster"]
    assert writes == [False]
    assert result.collect.outcomes[0].reason == "dry-run"
    assert result.collect.outcomes[0].source_type == "youtube"
    assert "commit" not in events
    assert "rollback" in events


def test_unknown_user_stops_before_normalize(monkeypatch, users):
    order, _writes = _patch_stages(monkeypatch)
    with pytest.raises(UnknownUserError):
        asyncio.run(run_pipeline("missing@example.com", session_factory=users))
    assert order == []


def test_fixture_normalizes_youtube_payloads_then_clusters():
    import json

    first = chain_payloads(json.loads(FIXTURE.read_text(encoding="utf-8")))
    second = chain_payloads(json.loads(FIXTURE.read_text(encoding="utf-8")))
    assert first.processed == 3
    assert first.failed == 0
    assert len(first.clusters) == 1
    assert first.clusters[0].primary_id in {1, 2}
    assert {member.feed_item_id for member in first.clusters[0].members} == {1, 2}
    assert first.unclustered == 1
    assert second.clusters[0].primary_id == first.clusters[0].primary_id
    assert [member.feed_item_id for member in second.clusters[0].members] == [
        member.feed_item_id for member in first.clusters[0].members
    ]


def test_cli_fixture_prints_cluster(capsys):
    assert main(["--fixture", str(FIXTURE)]) == 0
    output = capsys.readouterr().out
    assert "processed=3 failed=0" in output
    assert "clusters=1 unclustered=1" in output


def test_cli_unknown_user_exits_2(monkeypatch, capsys):
    async def fake_run(identifier, *, limit, dry_run):
        raise UnknownUserError(identifier)

    monkeypatch.setattr("app.pipeline.__main__.run_pipeline", fake_run)
    assert main(["--user", "missing@example.com"]) == 2
    assert "Unknown user" in capsys.readouterr().err


def test_cli_requires_user_or_fixture(capsys):
    assert main([]) == 2
    assert "Pass --user or --fixture" in capsys.readouterr().err


def test_cli_rejects_user_with_fixture(capsys):
    assert main(["--user", "1", "--fixture", str(FIXTURE)]) == 2
    assert "--user and --fixture" in capsys.readouterr().err


def test_cli_rejects_non_positive_limit():
    assert main(["--user", "1", "--limit", "0"]) == 2
