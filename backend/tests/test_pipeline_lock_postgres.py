"""Реальный PostgreSQL для advisory lock: env-gated disposable БД.

Запуск (одноразовая БД, не production):

    ITD84_TEST_DATABASE_URL='postgresql+psycopg://...' \
        pytest tests/test_pipeline_lock_postgres.py

Без переменной тесты пропускаются. Тест берёт только сессионные advisory locks и
соединения: схему не создаёт и не меняет, платных вызовов не делает. Так
проверяется реальный контракт SQLAlchemy 2.0 (unit-stub его не моделирует
полностью) — регресс порядка `execution_options`/`detach`, конкуренция,
освобождение и физическое закрытие выделенного соединения.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, text

from app.pipeline import lock as lock_module
from app.pipeline.lock import LOCK_CLASSID, LOCK_OBJID, PostgresAdvisoryLock

TEST_DATABASE_URL = os.environ.get("ITD84_TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="нужен одноразовый ITD84_TEST_DATABASE_URL (production БД не используется)",
)

_ADVISORY_LOCKS_SQL = text(
    "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' "
    "AND classid = CAST(:classid AS oid) AND objid = CAST(:objid AS oid) "
    "AND objsubid = 2"
)


@pytest.fixture
def engine():
    engine = create_engine(TEST_DATABASE_URL, pool_pre_ping=True, hide_parameters=True)
    yield engine
    engine.dispose()


def _advisory_locks(engine) -> int:
    with engine.connect() as connection:
        return connection.execute(
            _ADVISORY_LOCKS_SQL, {"classid": LOCK_CLASSID, "objid": LOCK_OBJID}
        ).scalar()


def test_autocommit_cannot_be_applied_after_detach(engine):
    """Причина исходного дефекта: после detach нет pool-записи для finalize."""
    connection = engine.connect()
    connection.detach()
    try:
        with pytest.raises(AttributeError, match="finalize_callback"):
            connection.execution_options(isolation_level="AUTOCOMMIT")
    finally:
        connection.close()


def test_lock_contention_release_and_dedicated_connection(engine):
    first = PostgresAdvisoryLock(engine)
    assert first.acquire() is True
    assert first.verify() is True
    assert _advisory_locks(engine) == 1

    second = PostgresAdvisoryLock(engine)
    assert second.acquire() is False
    assert second._connection is None
    assert _advisory_locks(engine) == 1

    first.release()
    assert first.verify() is False
    assert _advisory_locks(engine) == 0

    third = PostgresAdvisoryLock(engine)
    assert third.acquire() is True
    third.release()

    # Выделенные соединения не осели в пуле с захваченным lock.
    assert engine.pool.checkedout() == 0
    assert _advisory_locks(engine) == 0


def test_pid_failure_closes_locked_connection_and_releases_lock(engine, monkeypatch):
    """Сбой после успешного lock (чтение pid) закрывает соединение физически."""
    monkeypatch.setattr(lock_module, "_BACKEND_PID_SQL", text("SELECT 1 / 0"))
    lock = PostgresAdvisoryLock(engine)

    with pytest.raises(Exception):  # noqa: B017 - реальная ошибка драйвера
        lock.acquire()

    assert lock._connection is None
    assert lock.verify() is False
    assert _advisory_locks(engine) == 0
    assert engine.pool.checkedout() == 0
    lock.release()
    assert _advisory_locks(engine) == 0
