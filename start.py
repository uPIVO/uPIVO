"""Запуск окружения разработчика. Инструкция: START_HERE.md."""

import argparse
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BACKEND = ROOT / "backend"
BIN = "Scripts" if os.name == "nt" else "bin"
PYTHON = BACKEND / ".venv" / BIN / ("python.exe" if os.name == "nt" else "python")

# Ожидаем настоящие запросы к БД и Redis, а не просто открытые TCP-порты.
WAIT_FOR_SERVICES = """
import time
from redis import Redis
from sqlalchemy import text
from app.core.config import get_settings
from app.db.session import get_engine

deadline = time.monotonic() + 60
settings = get_settings()
redis = Redis.from_url(settings.redis_url.get_secret_value(),
                        socket_connect_timeout=2, socket_timeout=2)
try:
    while True:
        try:
            with get_engine().connect() as connection:
                connection.execute(text('SELECT 1'))
            redis.ping()
            break
        except Exception:
            if time.monotonic() >= deadline:
                raise SystemExit('БД или Redis недоступны. Проверьте порты и URL в .env, '
                                 'затем логи контейнеров. См. docs/TROUBLESHOOTING.md.')
            time.sleep(1)
finally:
    redis.close()
    get_engine().dispose()
"""


def run(command, *, cwd=ROOT, timeout=None):
    print(f"\n> {shlex.join(str(part) for part in command)}", flush=True)
    subprocess.run([str(part) for part in command], cwd=cwd, check=True, timeout=timeout)


def require_uv():
    uv = shutil.which("uv")
    if not uv:
        raise RuntimeError("Установите uv по docs/SETUP.md и откройте новый терминал.")
    return uv


def ensure_env():
    env = ROOT / ".env"
    if not env.exists():
        shutil.copyfile(ROOT / ".env.example", env)
        print("Создан .env из .env.example.", flush=True)


def sync_dependencies(uv):
    run([uv, "sync", "--locked", "--python", "3.12"], cwd=BACKEND)


def compose_command(uv=None):
    docker = shutil.which("docker")
    if docker:
        result = subprocess.run(
            [docker, "compose", "version"], capture_output=True, timeout=15, check=False
        )
        if result.returncode == 0:
            return [docker, "compose"]

    if shutil.which("podman"):
        provider = shutil.which("podman-compose")
        local_provider = (
            ROOT
            / ".tools"
            / "compose"
            / BIN
            / ("podman-compose.exe" if os.name == "nt" else "podman-compose")
        )
        if not provider and local_provider.exists():
            provider = str(local_provider)
        if not provider and uv:
            run([uv, "venv", ROOT / ".tools" / "compose", "--python", "3.12"])
            tool_python = local_provider.parent / ("python.exe" if os.name == "nt" else "python")
            run(
                [
                    uv,
                    "pip",
                    "install",
                    "--python",
                    tool_python,
                    "-r",
                    ROOT / "scripts" / "requirements-tools.txt",
                ]
            )
            provider = str(local_provider)
        if provider:
            return [provider, "-p", "upivo", "-f", str(ROOT / "docker-compose.yml")]

    raise RuntimeError(
        "Нужен запущенный Docker с Compose или Podman. См. docs/SETUP.md. "
        "Для API без БД используйте: python start.py --api-only"
    )


def serve():
    # Достаём настройки тем же способом, что и приложение (env процесса имеет приоритет).
    result = subprocess.run(
        [
            str(PYTHON),
            "-c",
            ("from app.core.config import get_settings; s = get_settings(); print(s.app_port)"),
        ],
        cwd=BACKEND,
        capture_output=True,
        text=True,
        check=True,
    )
    port = result.stdout.strip()
    print(f"\nSwagger: http://localhost:{port}/docs", flush=True)
    print("Пишите код в backend/app. Остановка API: Ctrl+C.", flush=True)
    print("Остановка БД и Redis: python start.py --stop", flush=True)
    run(
        [
            PYTHON,
            "-m",
            "uvicorn",
            "app.main:app",
            "--reload",
            "--host",
            "127.0.0.1",
            "--port",
            port,
        ],
        cwd=BACKEND,
    )


def main():
    parser = argparse.ArgumentParser(description="Подготовка и запуск Upivo для разработки.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--setup", action="store_true", help="Подготовить окружение без запуска API")
    mode.add_argument("--api-only", action="store_true", help="Запустить API без БД и Redis")
    mode.add_argument(
        "--check", action="store_true", help="Запустить ruff и pytest без контейнеров"
    )
    mode.add_argument(
        "--stop", action="store_true", help="Остановить БД и Redis, сохранив данные"
    )
    args = parser.parse_args()

    try:
        if args.stop:
            run([*compose_command(), "stop", "postgres", "redis"])
            return 0

        uv = require_uv()
        if not args.check:
            ensure_env()
        sync_dependencies(uv)
        if args.check:
            run(
                [
                    PYTHON,
                    "-m",
                    "ruff",
                    "check",
                    "--config",
                    "pyproject.toml",
                    ".",
                    "../start.py",
                ],
                cwd=BACKEND,
            )
            run([PYTHON, "-m", "pytest"], cwd=BACKEND)
            return 0

        if args.api_only:
            print(
                "БД и Redis не запускаются; /health/db требует доступной БД.",
                flush=True,
            )
        else:
            compose = compose_command(uv)
            run([*compose, "up", "-d", "postgres", "redis"])
            print("Ожидаю готовности PostgreSQL и Redis…", flush=True)
            subprocess.run(
                [str(PYTHON), "-c", WAIT_FOR_SERVICES],
                cwd=BACKEND,
                check=True,
                timeout=90,
            )
            run([PYTHON, "-m", "alembic", "upgrade", "head"], cwd=BACKEND)
        if args.setup:
            print("\nОкружение готово. Запуск API: python start.py", flush=True)
            return 0
        serve()
        return 0
    except KeyboardInterrupt:
        print("\nAPI остановлен. Остановка контейнеров: python start.py --stop")
        return 0
    except (RuntimeError, OSError, subprocess.TimeoutExpired) as error:
        print(f"\nНе удалось запустить проект: {error}", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as error:
        print(
            f"\nКоманда завершилась с кодом {error.returncode}. "
            "Подробности выше; помощь: docs/TROUBLESHOOTING.md.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
