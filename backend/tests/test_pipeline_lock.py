"""Advisory lock цепочки без PostgreSQL: выделенное соединение и освобождение.

Stub-соединение подменяет SQLAlchemy Connection, чтобы проверить контракт:
lock берётся на отдельном соединении с AUTOCOMMIT, соединение выводится из пула
(``detach``), проверка владения идёт запросом к ``pg_locks``, а ``release`` снимает
lock и физически закрывает соединение. Реальное поведение PostgreSQL проверяется
отдельным Compose-прогоном.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.pipeline.lock import (
    LOCK_CLASSID,
    LOCK_OBJID,
    PipelineLockLost,
    PostgresAdvisoryLock,
    ensure_lock,
)

_REVERSE = "reverse"


class _SAScalar:
    def __init__(self, value):
        self._value = value

    def scalar(self):
        return self._value


class _SAConnection:
    """Подмена SQLAlchemy Connection с AUTOCOMMIT и detach/close."""

    def __init__(
        self,
        log,
        *,
        acquired: bool = True,
        verify_count: int = 1,
        fail: str | None = None,
    ):
        self._log = log
        self._acquired = acquired
        self._verify_count = verify_count
        self._fail = fail
        self.closed = False
        self.detached = False
        self.released_to_pool = False

    def execution_options(self, **options):
        self._log.append(("execution_options", options))
        if self._fail == "execution_options":
            raise RuntimeError("не удалось включить AUTOCOMMIT")
        return self

    def execute(self, statement, params=None):
        sql = str(statement)
        self._log.append(("execute", sql, dict(params or {})))
        if "pg_try_advisory_lock" in sql:
            if self._fail == "try_lock":
                raise RuntimeError("запрос блокировки упал")
            return _SAScalar(self._acquired)
        if "pg_backend_pid" in sql:
            if self._fail == "pid":
                raise RuntimeError("запрос pid упал")
            return _SAScalar(4321)
        if "pg_locks" in sql:
            return _SAScalar(self._verify_count)
        if "pg_advisory_unlock" in sql:
            return _SAScalar(True)
        raise AssertionError(sql)

    def detach(self):
        self._log.append("detach")
        self.detached = True

    def close(self):
        self.closed = True
        # detach означает, что close закрывает соединение, а не возвращает в пул.
        self.released_to_pool = not self.detached
        self._log.append("close")


class _Engine:
    def __init__(
        self, log, *, dialect="postgresql", acquired=True, verify_count=1, fail=None
    ):
        self.dialect = SimpleNamespace(name=dialect)
        self._log = log
        self._acquired = acquired
        self._verify_count = verify_count
        self._fail = fail
        self.connections: list[_SAConnection] = []

    def connect(self):
        connection = _SAConnection(
            self._log,
            acquired=self._acquired,
            verify_count=self._verify_count,
            fail=self._fail,
        )
        self.connections.append(connection)
        return connection


def test_non_postgres_engine_raises_instead_of_skipping_lock():
    with pytest.raises(RuntimeError, match="требует PostgreSQL"):
        PostgresAdvisoryLock(_Engine([], dialect="sqlite"))


def test_acquire_detaches_before_lock_attempt_and_keeps_connection():
    log: list[object] = []
    engine = _Engine(log)
    lock = PostgresAdvisoryLock(engine)

    assert lock.acquire() is True

    assert ("execution_options", {"isolation_level": "AUTOCOMMIT"}) in log
    connection = engine.connections[0]
    assert connection.detached is True
    # detach выполняется до попытки захвата: соединение уже вне пула.
    detach_at = log.index("detach")
    lock_at = next(
        index
        for index, item in enumerate(log)
        if item[0] == "execute" and "pg_try" in item[1]
    )
    assert detach_at < lock_at
    # Соединение ещё живо: lock держится на нём до release.
    assert connection.closed is False
    params = next(item[2] for item in log if item[0] == "execute" and "pg_try" in item[1])
    assert params == {"classid": LOCK_CLASSID, "objid": LOCK_OBJID}


def test_busy_lock_closes_detached_connection_physically():
    log: list[object] = []
    engine = _Engine(log, acquired=False)
    lock = PostgresAdvisoryLock(engine)

    assert lock.acquire() is False
    connection = engine.connections[0]
    assert "detach" in log
    assert connection.closed is True
    # Занятая блокировка не возвращает выделенное соединение в пул.
    assert connection.released_to_pool is False
    # Нечего освобождать: lock не удерживается этим объектом.
    lock.release()
    assert log.count("close") == 1


def test_pid_query_failure_after_lock_closes_connection_physically():
    """Успешный lock + ошибка чтения pid не должны вернуть lock в пул."""
    log: list[object] = []
    engine = _Engine(log, fail="pid")
    lock = PostgresAdvisoryLock(engine)

    with pytest.raises(RuntimeError, match="pid"):
        lock.acquire()

    connection = engine.connections[0]
    assert connection.closed is True
    assert connection.released_to_pool is False
    # Объект не удерживает соединение: verify ничего не подтверждает.
    assert lock.verify() is False
    lock.release()
    assert log.count("close") == 1


def test_lock_query_failure_closes_connection_physically():
    log: list[object] = []
    engine = _Engine(log, fail="try_lock")
    lock = PostgresAdvisoryLock(engine)

    with pytest.raises(RuntimeError, match="блокировки"):
        lock.acquire()

    connection = engine.connections[0]
    assert connection.detached is True
    assert connection.closed is True
    assert connection.released_to_pool is False
    lock.release()
    assert log.count("close") == 1


def test_autocommit_configuration_failure_closes_connection_physically():
    log: list[object] = []
    engine = _Engine(log, fail="execution_options")
    lock = PostgresAdvisoryLock(engine)

    with pytest.raises(RuntimeError, match="AUTOCOMMIT"):
        lock.acquire()

    connection = engine.connections[0]
    assert connection.detached is True
    assert connection.closed is True
    assert connection.released_to_pool is False
    lock.release()
    assert log.count("close") == 1


def test_verify_checks_pg_locks_for_this_backend_pid():
    log: list[object] = []
    engine = _Engine(log)
    lock = PostgresAdvisoryLock(engine)
    assert lock.acquire() is True

    assert lock.verify() is True
    sql, params = next(
        (item[1], item[2]) for item in log if item[0] == "execute" and "pg_locks" in item[1]
    )
    assert "objsubid = 2" in sql
    assert params == {"classid": LOCK_CLASSID, "objid": LOCK_OBJID, "pid": 4321}


def test_release_unlocks_and_closes_the_dedicated_connection():
    log: list[object] = []
    engine = _Engine(log)
    lock = PostgresAdvisoryLock(engine)
    assert lock.acquire() is True
    connection = engine.connections[0]

    lock.release()

    unlock = next(item for item in log if item[0] == "execute" and "pg_advisory_unlock" in item[1])
    assert unlock[2] == {"classid": LOCK_CLASSID, "objid": LOCK_OBJID}
    assert connection.closed is True
    # Соединение не вернулось в пул с захваченным lock.
    assert connection.released_to_pool is False
    assert lock.verify() is False
    # Повторное освобождение безопасно.
    lock.release()


def test_ensure_lock_turns_connection_error_into_lock_lost():
    class BrokenLock:
        def acquire(self):
            return True

        def verify(self):
            raise RuntimeError("connection reset")

        def release(self):
            pass

    with pytest.raises(PipelineLockLost, match="разорвано"):
        ensure_lock(BrokenLock())


def test_ensure_lock_reports_lost_ownership():
    class LostLock:
        def acquire(self):
            return True

        def verify(self):
            return False

        def release(self):
            pass

    with pytest.raises(PipelineLockLost, match="не принадлежит"):
        ensure_lock(LostLock())
