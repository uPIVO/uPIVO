"""Upgrade existing main data without rewriting snapshots or personal records."""

import os

import pytest
from sqlalchemy import text

from tests.test_digest_snapshot_migration_postgres import (
    _alembic,
    _all_user_data,
    _seed_legacy_digests,
    migration_database,  # noqa: F401 -- shared isolated database fixture
)

pytestmark = pytest.mark.skipif(
    not os.environ.get("ITD17_TEST_DATABASE_URL"), reason="Requires isolated PostgreSQL"
)


def test_sprint3_upgrade_preserves_main_data_and_digest_snapshots(migration_database):  # noqa: F811
    engine, url = migration_database
    assert _alembic(url, "upgrade", "0009_content_unit_ai_analysis").returncode == 0
    _seed_legacy_digests(engine)
    assert _alembic(url, "upgrade", "0010_digest_items_snapshot").returncode == 0
    before = _all_user_data(engine)
    with engine.connect() as conn:
        snapshots = conn.execute(text("SELECT id, items_snapshot FROM digests ORDER BY id")).all()
    result = _alembic(url, "upgrade", "head")
    assert result.returncode == 0, result.stderr
    after = _all_user_data(engine)
    assert {table: after[table] for table in before} == before
    with engine.connect() as conn:
        assert (
            conn.execute(text("SELECT id, items_snapshot FROM digests ORDER BY id")).all()
            == snapshots
        )
        assert conn.scalars(text("SELECT version_num FROM alembic_version")).all() == [
            "0015_digest_settings"
        ]
    # New tables are empty; downgrading just the sprint is safe for existing main data.
    result = _alembic(url, "downgrade", "0010_digest_items_snapshot")
    assert result.returncode == 0, result.stderr
    assert _all_user_data(engine) == before
    result = _alembic(url, "upgrade", "head")
    assert result.returncode == 0, result.stderr
