"""Инициализация дочерних процессов Celery при spawn.

Celery 5.6.3 и billiard 4.3.0 на macOS/Python 3.12 запускают процессы пула через
spawn. В `celery.concurrency.prefork.process_initializer` штатные оптимизации
трассировки (`celery.app.trace.setup_worker_optimizations`) включаются только
тогда, когда billiard перезапустил процесс через `execv`
(`FORKED_BY_MULTIPROCESSING`). При spawn ребёнок импортирует модули заново,
`trace._localized` остаётся пустым, и `celery.app.trace.fast_trace_task` падает
до выполнения задачи:

    ValueError: not enough values to unpack (expected 3, got 0)

Проект подключает штатный сигнал `worker_process_init`: при фактическом
`billiard.get_start_method() == "spawn"` дочерний процесс вызывает тот же
публичный `setup_worker_optimizations`, что и fork-ветка Celery. Это не патч
Celery и не смена пула: при fork/forkserver обработчик ничего не делает, а
`--pool=solo` не требуется.

Оптимизации применяются к текущему app дочернего процесса: это app, который
инициализировал пул и чьи задачи `process_initializer` уже снабдил tracer'ами.
Имя узла берётся по умолчанию (`celery@<host>`); при явном `celery -n` это
влияет только на `task.request.hostname` в логах и событиях.
"""

from __future__ import annotations

import logging

from celery import current_app
from celery.app import trace
from celery.signals import worker_process_init
from celery.utils.nodenames import default_nodename

logger = logging.getLogger(__name__)


def worker_process_start_method() -> str:
    """Фактический метод старта дочерних процессов billiard в этом процессе."""
    import billiard

    return billiard.get_start_method()


def init_spawned_worker_process(app=None, *, hostname: str | None = None) -> bool:
    """Включить trace-оптимизации Celery в spawn-ребёнке пула.

    Возвращает True, если оптимизации включены. При fork/forkserver штатная
    инициализация Celery уже корректна, повтор не нужен.
    """
    if worker_process_start_method() != "spawn":
        return False
    target = app if app is not None else current_app._get_current_object()
    trace.setup_worker_optimizations(target, hostname or default_nodename(None))
    return True


@worker_process_init.connect(weak=False)
def _worker_process_init(**_: object) -> None:
    """Обработчик штатного сигнала Celery `worker_process_init`."""
    if init_spawned_worker_process():
        logger.debug("Celery spawn: trace-оптимизации включены в дочернем процессе")
