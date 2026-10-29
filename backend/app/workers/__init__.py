"""Фоновые задачи Celery и инициализация дочерних процессов пула.

Импорт пакета регистрирует обработчик `worker_process_init`
(`app.workers.worker_init`): без него spawn-дети Celery на macOS/Python 3.12
падают в `fast_trace_task` до выполнения задачи.
"""

from app.workers import worker_init  # noqa: F401 - регистрация worker_process_init

__all__ = ["worker_init"]
