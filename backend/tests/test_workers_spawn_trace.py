"""Проверки совместимости Celery-пула со spawn на macOS/Python 3.12.

Unit-тесты закрывают guard «только spawn» и регистрацию сигнала, а
`tests/spawn_probe` даёт фактическое исполнение задачи в реальном дочернем
процессе billiard: до исправления — та же ошибка распаковки `_localized`, после
— noop-задача выполняется и возвращает ожидаемое значение. Ни Redis, ни БД, ни AI
не используются.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from celery import current_app
from celery.signals import worker_process_init

from app.workers import worker_init
from tests.spawn_probe import PROBE_RESULT, run_spawn_probe

BACKEND = Path(__file__).resolve().parents[1]


def test_init_skips_non_spawn_start_methods(monkeypatch):
    calls: list[tuple[object, str]] = []
    monkeypatch.setattr(
        worker_init.trace,
        "setup_worker_optimizations",
        lambda app, hostname: calls.append((app, hostname)),
    )

    for method in ("fork", "forkserver"):
        monkeypatch.setattr(worker_init, "worker_process_start_method", lambda m=method: m)
        assert worker_init.init_spawned_worker_process(app=object()) is False

    assert calls == []


def test_init_calls_setup_for_spawn_with_defaults(monkeypatch):
    recorded: dict[str, object] = {}
    monkeypatch.setattr(worker_init, "worker_process_start_method", lambda: "spawn")
    monkeypatch.setattr(
        worker_init.trace,
        "setup_worker_optimizations",
        lambda app, hostname: recorded.update(app=app, hostname=hostname),
    )

    assert worker_init.init_spawned_worker_process() is True

    assert recorded["app"] is current_app._get_current_object()
    assert str(recorded["hostname"]).startswith("celery@")


def test_init_passes_explicit_app_and_hostname(monkeypatch):
    recorded: dict[str, object] = {}
    target = object()
    monkeypatch.setattr(worker_init, "worker_process_start_method", lambda: "spawn")
    monkeypatch.setattr(
        worker_init.trace,
        "setup_worker_optimizations",
        lambda app, hostname: recorded.update(app=app, hostname=hostname),
    )

    assert worker_init.init_spawned_worker_process(target, hostname="celery@test") is True
    assert recorded == {"app": target, "hostname": "celery@test"}


def test_signal_handler_is_connected_without_weak_reference():
    assert worker_process_init.has_listeners()
    strong = [
        receiver
        for _lookup_key, receiver in worker_process_init.receivers
        if receiver is worker_init._worker_process_init
    ]
    assert strong, "worker_process_init должен быть подключён strong-ссылкой (weak=False)"


def test_import_has_no_optimization_side_effects():
    code = (
        "import sys\n"
        "import app.workers.worker_init as module\n"
        "from celery.app import trace\n"
        "print('localized', len(trace._localized))\n"
        "print('settings', 'app.core.config' in sys.modules)\n"
        "print('handler', callable(module._worker_process_init))\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(BACKEND),
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    values = dict(line.split() for line in completed.stdout.strip().splitlines())
    # Импорт обработчика не читает Settings и не трогает trace-оптимизации.
    assert values == {"localized": "0", "settings": "False", "handler": "True"}


def test_spawn_child_without_handler_reproduces_fast_trace_failure(tmp_path):
    result = run_spawn_probe("without_handler", tmp_path / "without.json")

    assert result["localized_len"] == 0
    assert result["use_fast_trace"] is False
    assert "error" in result
    assert "ValueError" in result["error"]
    assert "not enough values to unpack" in result["error"]
    assert result.get("executed") is not True


def test_spawn_child_with_handler_executes_task(tmp_path):
    result = run_spawn_probe("with_handler", tmp_path / "with.json")

    assert "error" not in result
    assert result["localized_len"] == 3
    assert result["use_fast_trace"] is True
    assert result["tasks_traced"] is True
    assert result["executed"] is True
    assert PROBE_RESULT in str(result["returned"])
