"""Межпроцессная блокировка полного общего прохода конвейера.

Одновременно выполняется не более одного полного прохода среди всех worker'ов и
хостов (ITD-84, раздел 3.3). Блокировка — сессионный advisory lock PostgreSQL
(``pg_try_advisory_lock``) на выделенном физическом соединении в режиме
AUTOCOMMIT: это не длинная транзакция, поэтому HTTP-вызовы источников и AI не
держат транзакцию БД.

Границы гарантии: lock живёт в PostgreSQL и снимается при завершении сессионного
соединения, то есть при падении процесса. Это координация через общий сервер БД,
а не распределённый fencing: при сетевом разделении между worker'ом и PostgreSQL
процесс теряет соединение вместе с блокировкой, но уже начатые внешние HTTP-вызовы
отменить невозможно. Потеря блокировки проверяется между стадиями и watchdog'ом во
время ожидания асинхронной стадии; после потери автоматического повторного захвата
нет, и проход не продолжается на следующей стадии.

Строгого прерывания синхронной стадии нет: сбор источников (Telegram/Reddit/
YouTube/Habr) — блокирующие вызовы внутри coroutine, они не отдают управление
event loop, поэтому watchdog не может сработать, пока такой вызов не вернётся.
Практически это значит, что после потери блокировки успевают завершиться текущие
синхронные вызовы источников (в том числе несколько подряд, до первой проверки
между стадиями), и обнаружение потери не эквивалентно отмене уже выполняемой
работы. Абсолютной гарантии «ровно один проход в любой момент» при сетевом
разделении нет; дополнительный контроль требует отдельной архитектуры
изолированных стадий и в ITD-84 не вводится.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable
from typing import Protocol

from sqlalchemy import Engine, text

logger = logging.getLogger(__name__)

# Фиксированное пространство имён проекта: (classid, objid) = «UPIV», 84.
LOCK_CLASSID = 0x55504956
LOCK_OBJID = 84
# Как часто watchdog проверяет живость блокировки во время асинхронной стадии.
LOCK_WATCHDOG_INTERVAL_SECONDS = 30.0

_ACQUIRE_SQL = text("SELECT pg_try_advisory_lock(:classid, :objid)")
_BACKEND_PID_SQL = text("SELECT pg_backend_pid()")
# ``pg_locks.classid/objid`` имеют тип oid, поэтому параметры явно приводятся.
_VERIFY_SQL = text(
    "SELECT count(*) FROM pg_locks "
    "WHERE locktype = 'advisory' AND classid = CAST(:classid AS oid) "
    "AND objid = CAST(:objid AS oid) AND objsubid = 2 AND pid = :pid AND granted"
)
_UNLOCK_SQL = text("SELECT pg_advisory_unlock(:classid, :objid)")


class PipelineLockBusy(RuntimeError):
    """Полный общий проход уже выполняет другой процесс (``skipped_locked``)."""


class PipelineLockLost(RuntimeError):
    """Блокировка потеряна: соединение разорвано или lock снят другой стороной."""


class RunLock(Protocol):
    """Контракт блокировки прохода; тесты подставляют свою реализацию."""

    def acquire(self) -> bool:
        """Захватить блокировку без ожидания; ``False`` — она занята."""
        ...

    def verify(self) -> bool:
        """Подтвердить, что блокировка всё ещё принадлежит этому соединению."""
        ...

    def release(self) -> None:
        """Снять блокировку и закрыть выделенное соединение."""
        ...


class PostgresAdvisoryLock:
    """Сессионный advisory lock PostgreSQL на выделенном соединении вне пула.

    Соединение создаётся с ``AUTOCOMMIT``, поэтому захват и проверка не открывают
    транзакцию. ``detach`` выполняется до попытки захвата: любой исход (занято,
    ошибка настройки режима, ошибка запроса) закрывает выделенное соединение
    физически в ``finally``, и пул не получит его обратно вместе с lock. После
    успешного захвата соединение удерживается этим объектом, а освобождение —
    ``pg_advisory_unlock`` и закрытие в ``finally``; при разрыве связи сервер сам
    снимает lock.
    """

    def __init__(
        self,
        engine: Engine,
        *,
        classid: int = LOCK_CLASSID,
        objid: int = LOCK_OBJID,
    ) -> None:
        if engine.dialect.name != "postgresql":
            raise RuntimeError(
                "Блокировка общего прохода требует PostgreSQL, получен диалект "
                f"{engine.dialect.name!r}. Для тестов без PostgreSQL передайте "
                "явную реализацию RunLock, а не отключайте блокировку."
            )
        # Двухаргументная форма pg_try_advisory_lock работает с int4: значения
        # вне диапазона дали бы другую функцию и другой ключ блокировки.
        if not (0 <= classid < 2**31 and 0 <= objid < 2**31):
            raise ValueError("classid и objid должны помещаться в int4")
        self._engine = engine
        self._classid = classid
        self._objid = objid
        self._connection = None
        self._pid: int | None = None

    def acquire(self) -> bool:
        if self._connection is not None:
            raise RuntimeError("Этот объект блокировки уже захвачен")
        connection = None
        detached = False
        try:
            connection = self._engine.connect()
            # AUTOCOMMIT настраивается ДО detach: в SQLAlchemy 2.0
            # ``execution_options(isolation_level=...)`` пишет finalize callback в
            # pool-запись соединения, а после ``detach()`` её уже нет
            # (AttributeError на реальном движке). Lock до detach не захвачен,
            # поэтому утечки захваченного соединения в пул этот порядок не даёт.
            connection.execution_options(isolation_level="AUTOCOMMIT")
            # detach до попытки захвата: с этого момента любой исход закрывает
            # выделенное соединение физически, а не возвращает его в пул.
            connection.detach()
            detached = True
            acquired = bool(
                connection.execute(
                    _ACQUIRE_SQL, {"classid": self._classid, "objid": self._objid}
                ).scalar()
            )
            if not acquired:
                return False
            pid = connection.execute(_BACKEND_PID_SQL).scalar()
            self._connection = connection
            self._pid = int(pid) if pid is not None else None
        finally:
            if self._connection is None and connection is not None:
                with contextlib.suppress(Exception):
                    if detached:
                        # Соединение уже выведено из пула: close физически
                        # закрывает его (в том числе после ошибок acquire/pid).
                        connection.close()
                    else:
                        # Сбой до detach: invalidate помечает DBAPI-соединение
                        # непригодным, и close не возвращает его в пул.
                        connection.invalidate()
                        connection.close()
        logger.info(
            "Захвачена блокировка общего прохода classid=%s objid=%s pid=%s",
            self._classid,
            self._objid,
            self._pid,
        )
        return True

    def verify(self) -> bool:
        connection = self._connection
        if connection is None or self._pid is None:
            return False
        count = connection.execute(
            _VERIFY_SQL,
            {"classid": self._classid, "objid": self._objid, "pid": self._pid},
        ).scalar()
        return bool(count)

    def release(self) -> None:
        connection, self._connection = self._connection, None
        pid, self._pid = self._pid, None
        if connection is None:
            return
        try:
            unlocked = connection.execute(
                _UNLOCK_SQL, {"classid": self._classid, "objid": self._objid}
            ).scalar()
            if unlocked is False:
                logger.warning(
                    "Блокировка общего прохода уже не принадлежала соединению pid=%s", pid
                )
        except Exception:
            logger.warning(
                "Не удалось снять блокировку общего прохода pid=%s; соединение закрывается",
                pid,
                exc_info=True,
            )
        finally:
            # Соединение было detached: close физически закрывает его, а не
            # возвращает в пул.
            with contextlib.suppress(Exception):
                connection.close()
        logger.info("Блокировка общего прохода освобождена pid=%s", pid)


def ensure_lock(lock: RunLock | None) -> None:
    """Проверить блокировку между стадиями; потеря прерывает проход."""
    if lock is None:
        return
    try:
        alive = lock.verify()
    except Exception as exc:
        raise PipelineLockLost(
            "Блокировка общего прохода потеряна: соединение с PostgreSQL разорвано. "
            "Повторный захват не выполняется."
        ) from exc
    if not alive:
        raise PipelineLockLost(
            "Блокировка общего прохода потеряна: lock больше не принадлежит этому "
            "соединению. Повторный захват не выполняется."
        )


async def run_with_lock_watchdog[T](
    lock: RunLock | None,
    awaitable: Awaitable[T],
    *,
    interval: float = LOCK_WATCHDOG_INTERVAL_SECONDS,
) -> T:
    """Дождаться асинхронной стадии, проверяя блокировку по таймеру.

    Сетевая стадия может идти дольше, чем требуется на проверку соединения,
    поэтому живость блокировки проверяется не только между стадиями. При потере
    блокировки стадия отменяется и проход прерывается: продолжать без
    исключительности нельзя.
    """
    if lock is None:
        return await awaitable
    task = asyncio.ensure_future(awaitable)
    try:
        while True:
            done, _pending = await asyncio.wait({task}, timeout=interval)
            if done:
                return task.result()
            ensure_lock(lock)
    finally:
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
