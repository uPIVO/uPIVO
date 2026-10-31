"""Digest settings races and migration integrity on disposable PostgreSQL."""

import os
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.models import User, UserDigestSetting
from app.schemas.digest import DigestSettingsUpdateRequest
from app.services.digest import DigestService
from tests.test_feedback_postgres import _alembic, _test_database

pytestmark = pytest.mark.skipif(
    not os.environ.get("ITD89_TEST_DATABASE_URL"), reason="Requires disposable PostgreSQL"
)


def test_concurrent_first_settings_updates_preserve_both_fields():
    with _test_database("head") as database:
        with Session(database.engine) as db, db.begin():
            user = User(email="settings@example.test", password_hash="test-only")
            db.add(user)
            db.flush()
            uid = user.id
        barrier = Barrier(2)

        class RacingSession(Session):
            checked = False

            def get(self, entity, ident, **kwargs):
                result = super().get(entity, ident, **kwargs)
                if entity is UserDigestSetting and not self.checked:
                    self.checked = True
                    assert result is None
                    barrier.wait(timeout=10)
                return result

        def update(values):
            with RacingSession(database.engine) as db:
                return DigestService(db).update_settings(uid, DigestSettingsUpdateRequest(**values))

        with ThreadPoolExecutor(max_workers=2) as executor:
            list(executor.map(update, [{"period_hours": 72}, {"max_items": 3}]))
        with Session(database.engine) as db:
            rows = db.scalars(select(UserDigestSetting)).all()
            assert len(rows) == 1
            assert (rows[0].period_hours, rows[0].max_items) == (72, 3)


def test_settings_migration_roundtrip_preserves_existing_user():
    with _test_database("0014_sprint3_heads") as database:
        with database.engine.begin() as conn:
            uid = conn.scalar(
                text(
                    "INSERT INTO users (email,password_hash) VALUES "
                    "('migration-settings@example.test','test-only') RETURNING id"
                )
            )
        _alembic(database.url, "upgrade", "head")
        with database.engine.begin() as conn:
            conn.execute(
                text("INSERT INTO user_digest_settings (user_id) VALUES (:id)"), {"id": uid}
            )
            assert conn.execute(
                text("SELECT period_hours,max_items FROM user_digest_settings")
            ).one() == (24, 15)
        _alembic(database.url, "check")
        _alembic(database.url, "downgrade", "0014_sprint3_heads")
        with database.engine.connect() as conn:
            assert conn.scalar(text("SELECT count(*) FROM users WHERE id=:id"), {"id": uid}) == 1
        _alembic(database.url, "upgrade", "head")
        with database.engine.begin() as conn:
            conn.execute(
                text("INSERT INTO user_digest_settings (user_id) VALUES (:id)"), {"id": uid}
            )
            conn.execute(text("DELETE FROM users WHERE id=:id"), {"id": uid})
            assert conn.scalar(text("SELECT count(*) FROM user_digest_settings")) == 0
