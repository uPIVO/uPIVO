"""Check the snapshot migration on an isolated database containing legacy digests."""

import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import MetaData, Table, create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.models import ClusterItem, ContentCluster, FeedItem, Source, SourceType, User, UserSource
from app.models.digest import DigestItem
from app.models.enums import ProcessingStatus
from app.services.digest import DigestService

_URL = os.environ.get("ITD17_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not _URL, reason="Нужна отдельная тестовая PostgreSQL")
_BACKEND = Path(__file__).resolve().parents[1]
_PREVIOUS_REVISION = "0009_content_unit_ai_analysis"
_SNAPSHOT_REVISION = "0010_digest_items_snapshot"


def _alembic(url, command, revision):
    environment = os.environ.copy()
    environment["DATABASE_URL"] = url.render_as_string(hide_password=False)
    return subprocess.run(
        [sys.executable, "-m", "alembic", command, revision],
        cwd=_BACKEND,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
    )


@pytest.fixture
def migration_database():
    parent_url = make_url(_URL)
    assert parent_url.database.startswith("itd17_cluster_test"), "Нельзя использовать рабочую БД"
    database_name = f"itd17_cluster_test_snapshot_{uuid4().hex[:12]}"
    url = parent_url.set(database=database_name)
    admin = create_engine(parent_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{database_name}"'))
    engine = create_engine(url)
    try:
        yield engine, url
    finally:
        engine.dispose()
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE "{database_name}" WITH (FORCE)'))
        admin.dispose()


def _all_user_data(engine):
    """Compare every application table, including data unrelated to digests."""
    result = {}
    with engine.connect() as connection:
        for table_name in inspect(connection).get_table_names():
            if table_name == "alembic_version":
                continue
            rows = connection.execute(text(f'SELECT * FROM "{table_name}"')).mappings()
            values = []
            for row in rows:
                value = dict(row)
                value.pop("items_snapshot", None)
                values.append(value)
            result[table_name] = sorted(values, key=repr)
    return result


def _seed_legacy_digests(engine):
    now = datetime(2026, 10, 26, 12)
    long_summary = "  Полная выжимка: " + "важные подробности " * 30 + "  "
    digests = Table("digests", MetaData(), autoload_with=engine)
    with Session(engine) as session, session.begin():
        users = [User(email=f"legacy-{i}@example.test", password_hash="test") for i in range(2)]
        sources = [
            Source(
                type=source_type,
                name=name,
                external_id=f"legacy-{i}",
                canonical_url=f"https://example.test/source/{i}",
            )
            for i, (source_type, name) in enumerate(
                [(SourceType.HABR, "Хабр"), (SourceType.TELEGRAM, "Телеграм")]
            )
        ]
        session.add_all(users + sources)
        session.flush()
        session.add_all(
            UserSource(user_id=user.id, source_id=source.id, enabled=user.id == users[0].id)
            for user in users
            for source in sources
        )
        items = [
            FeedItem(
                source_id=sources[i % 2].id,
                external_id=f"legacy-{i}",
                title=f"Публикация {i}" if i != 2 else None,
                body_text="Исходный текст",
                original_url=f"https://example.test/post/{i}",
                published_at=now - timedelta(hours=i),
                processing_status=ProcessingStatus.NORMALIZED,
                summary=long_summary if i == 2 else f"Выжимка {i}",
                importance_score=9 - i if i != 1 else 10,
            )
            for i in range(3)
        ]
        cluster = ContentCluster(title=" Общий сюжет ", summary=" Общая сводка ")
        session.add_all(items + [cluster])
        session.flush()
        session.add_all(
            ClusterItem(cluster_id=cluster.id, feed_item_id=item.id, is_primary=i == 0)
            for i, item in enumerate(items[:2])
        )
        digest_ids = []
        for i in range(3):
            digest_id = session.execute(
                digests.insert()
                .values(
                    user_id=users[i % 2].id,
                    type="general",
                    title=f"Старый дайджест {i}",
                    summary_text=f"Неизменяемый текст {i}",
                    period_start=now - timedelta(days=7),
                    period_end=now,
                    items_count=2 if i == 0 else int(i == 2),
                    is_favorite=i == 2,
                    created_at=now - timedelta(days=5),
                )
                .returning(digests.c.id)
            ).scalar_one()
            digest_ids.append(digest_id)
            selected_items = items if i == 0 else (items[2:] if i == 2 else [])
            session.add_all(
                DigestItem(digest_id=digest_id, feed_item_id=item.id) for item in selected_items
            )
        return {
            "digest_ids": digest_ids,
            "user_id": users[0].id,
            "primary_id": items[0].id,
            "standalone_id": items[2].id,
            "long_summary": long_summary,
        }


def test_snapshot_upgrade_preserves_existing_user_data(migration_database):
    engine, url = migration_database
    prepared = _alembic(url, "upgrade", _PREVIOUS_REVISION)
    assert prepared.returncode == 0, prepared.stderr
    seeded = _seed_legacy_digests(engine)
    before = _all_user_data(engine)

    # A mid-backfill failure must roll back both earlier writes and the new column.
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE FUNCTION block_snapshot_update() RETURNS trigger LANGUAGE plpgsql AS $$ "
                "BEGIN IF NEW.title = 'Старый дайджест 1' THEN "
                "RAISE EXCEPTION 'simulated backfill failure'; END IF; RETURN NEW; END $$"
            )
        )
        connection.execute(
            text(
                "CREATE TRIGGER block_snapshot_update BEFORE UPDATE ON digests "
                "FOR EACH ROW EXECUTE FUNCTION block_snapshot_update()"
            )
        )
    failed_upgrade = _alembic(url, "upgrade", _SNAPSHOT_REVISION)
    assert failed_upgrade.returncode != 0
    assert "simulated backfill failure" in failed_upgrade.stderr
    assert "items_snapshot" not in {
        column["name"] for column in inspect(engine).get_columns("digests")
    }
    assert _all_user_data(engine) == before
    with engine.begin() as connection:
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == (
            _PREVIOUS_REVISION
        )
        connection.execute(text("DROP TRIGGER block_snapshot_update ON digests"))
        connection.execute(text("DROP FUNCTION block_snapshot_update()"))

    upgraded = _alembic(url, "upgrade", _SNAPSHOT_REVISION)
    assert upgraded.returncode == 0, upgraded.stderr
    assert _all_user_data(engine) == before

    with engine.connect() as connection:
        snapshots = dict(connection.execute(text("SELECT id, items_snapshot FROM digests")).all())
    populated, empty, favorite = seeded["digest_ids"]
    assert snapshots[empty] == []
    assert len(snapshots[populated]) == 2
    cluster, standalone = snapshots[populated]
    assert cluster["id"] == seeded["primary_id"]
    assert cluster["title"] == "Общий сюжет"
    assert cluster["summary"] == "Общая сводка"
    assert cluster["items_count"] == 2
    assert {source["name"] for source in cluster["related_sources"]} == {"Хабр", "Телеграм"}
    assert standalone["id"] == seeded["standalone_id"]
    assert standalone["title"] == "Без названия"
    assert standalone["summary"] == seeded["long_summary"]
    assert snapshots[favorite] == [standalone]
    assert not next(
        column
        for column in inspect(engine).get_columns("digests")
        if column["name"] == "items_snapshot"
    )["nullable"]

    with Session(engine) as session:
        original = DigestService(session).get_digest_detail(seeded["user_id"], populated)
        original_json = original.model_dump(mode="json")
    with engine.begin() as connection:
        connection.execute(text("UPDATE content_clusters SET title = 'Другой сюжет'"))
        connection.execute(text("UPDATE sources SET name = 'Новое название'"))
        connection.execute(text("UPDATE feed_items SET summary = 'Другой текст'"))
        connection.execute(text("DELETE FROM cluster_items"))
    with Session(engine) as session:
        saved = DigestService(session).get_digest_detail(seeded["user_id"], populated)
        assert saved.model_dump(mode="json") == original_json

    before_failed_downgrade = _all_user_data(engine)
    downgraded = _alembic(url, "downgrade", _PREVIOUS_REVISION)
    assert downgraded.returncode != 0
    assert "существующие дайджесты потеряют" in downgraded.stderr
    assert _all_user_data(engine) == before_failed_downgrade
    with engine.connect() as connection:
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == (
            _SNAPSHOT_REVISION
        )
        assert (
            dict(connection.execute(text("SELECT id, items_snapshot FROM digests")).all())
            == snapshots
        )
