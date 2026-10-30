"""Изолированный probe spawn-пула Celery: без Redis, БД, AI и пользовательских очередей.

Probe повторяет реальную инициализацию дочернего процесса
(`celery.concurrency.prefork.process_initializer`) в фактическом spawn-процессе
billiard и выполняет noop-задачу через `celery.app.trace.fast_trace_task`.
`without_handler` воспроизводит состояние до исправления: обработчик проекта
отключён в ребёнке, поэтому `trace._localized` остаётся пустым.
"""

from __future__ import annotations

import importlib
import json
import uuid
from pathlib import Path
from typing import Any

PROBE_TASK_NAME = "tests.spawn_probe.noop"
PROBE_HOSTNAME = "celery@probe"
PROBE_RESULT = "spawn-probe-ok"
EXECUTED: list[str] = []


def _noop(*_args: Any, **_kwargs: Any) -> str:
    EXECUTED.append(PROBE_RESULT)
    return PROBE_RESULT


def register_probe_task(app: Any) -> Any:
    """Зарегистрировать noop-задачу в конкретном app: без backend и побочных эффектов."""
    return app.task(name=PROBE_TASK_NAME, ignore_result=True, shared=False)(_noop)


def probe_child(pool_app: Any, hostname: str, mode: str, out_path: str) -> None:
    """Тело spawn-ребёнка: штатная инициализация Celery и проверка fast trace."""
    result: dict[str, Any] = {"mode": mode}
    try:
        if mode == "without_handler":
            # Состояние до исправления: обработчик проекта в ребёнке отключён.
            importlib.import_module("app.workers.tasks")
            from celery.signals import worker_process_init

            from app.workers.worker_init import _worker_process_init

            worker_process_init.disconnect(_worker_process_init)
        register_probe_task(pool_app)
        from celery.concurrency.prefork import process_initializer

        # Здесь Celery отправляет worker_process_init и строит tracer'ы задач.
        process_initializer(pool_app, hostname)
        from celery._state import get_current_app
        from celery.app import trace

        current = get_current_app()
        result["localized_len"] = len(trace._localized)
        result["use_fast_trace"] = bool(getattr(current, "use_fast_trace_task", False))
        result["tasks_traced"] = all(
            task.__trace__ is not None for task in current.tasks.values()
        )
        state, returned, _runtime = trace.fast_trace_task(
            PROBE_TASK_NAME,
            str(uuid.uuid4()),
            {"id": str(uuid.uuid4()), "task": PROBE_TASK_NAME},
            json.dumps([[], {}, {}]),
            "application/json",
            "utf-8",
        )
        result["state"] = state
        result["returned"] = returned
        result["executed"] = PROBE_RESULT in EXECUTED
    except BaseException as exc:  # noqa: BLE001 - probe сообщает класс и текст ошибки
        result["error"] = f"{type(exc).__name__}: {exc}"
    Path(out_path).write_text(json.dumps(result, ensure_ascii=False, default=str))


def run_spawn_probe(mode: str, out_path: Path) -> dict[str, Any]:
    """Родительская часть probe: настоящий spawn-ребёнок billiard с Celery initializer.

    Используется отдельный процесс (`Process`), а не пул: пул бесконечно
    перезапускал бы умершего ребёнка, а probe должен завершаться предсказуемо.
    """
    importlib.import_module("app.workers.tasks")
    from billiard import get_context
    from celery.app import trace

    from app.workers.celery_app import celery_app

    trace.setup_worker_optimizations(celery_app, PROBE_HOSTNAME)
    context = get_context("spawn")
    process = context.Process(
        target=probe_child,
        args=(celery_app, PROBE_HOSTNAME, mode, str(out_path)),
    )
    process.start()
    process.join(120)
    if process.is_alive():
        process.terminate()
        process.join(10)
        raise AssertionError(f"spawn-probe не завершился за 120 с (mode={mode})")
    if not out_path.exists():
        raise AssertionError(
            f"spawn-probe не записал результат (mode={mode}, exitcode={process.exitcode})"
        )
    return json.loads(out_path.read_text(encoding="utf-8"))
