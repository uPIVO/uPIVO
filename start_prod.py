"""Единая команда запуска демостенда. Инструкция: docs/LOCAL_DEMO.md."""

import argparse
import contextlib
import json
import os
import plistlib
import re
import shlex
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MAIN_COMPOSE = ROOT / "docker-compose.yml"
GATEWAY_COMPOSE = ROOT / "gateway" / "compose.yml"
PLIST_TEMPLATE = ROOT / "gateway" / "local.upivo-gateway.ngrok.plist.example"

MAIN_PROJECT = "upivo-demo"
GATEWAY_PROJECT = "upivo-gateway"
NEW_LABEL = "local.upivo-gateway.ngrok"
LEGACY_NGROK_LABEL = "local.doska-chatgpt.ngrok"

LOCAL_GATEWAY = "http://127.0.0.1:18080"
GATEWAY_UPSTREAM = "http://127.0.0.1:18080"
NGROK_TUNNELS = "http://127.0.0.1:4040/api/tunnels"
EXPECTED_API_PORT = "18000"
EXPECTED_GATEWAY_PORT = "18080"
PUBLIC_HEADERS = {"ngrok-skip-browser-warning": "1"}
DOCKER_WAIT_SECONDS = 120
READY_SECONDS = 120


class LauncherError(RuntimeError):
    """Ошибка, после которой запуск прекращается с ненулевым кодом."""


def _run(command, *, timeout=60, cwd=None):
    parts = [str(part) for part in command]
    print(f"> {shlex.join(parts)}", flush=True)
    try:
        return subprocess.run(
            parts,
            cwd=str(cwd or ROOT),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(parts, 124, "", f"таймаут {timeout} с")


def _http_status(url, *, headers=None, timeout=3.0):
    request = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response.read(1)
            return response.status
    except (urllib.error.URLError, OSError, ValueError):
        return None


def _json(url, *, timeout=3.0):
    try:
        with urllib.request.urlopen(urllib.request.Request(url), timeout=timeout) as response:
            if response.status != 200:
                return None
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return None


def _which(name):
    return shutil.which(name)


def _home():
    return Path.home()


def _env_file():
    return ROOT / ".env"


def _tools_dir():
    return ROOT / ".tools"


def _uid():
    return os.getuid()


def _plist_path():
    return _home() / "Library" / "LaunchAgents" / f"{NEW_LABEL}.plist"


def _log_path():
    return _home() / "Library" / "Logs" / "upivo-gateway" / "ngrok.log"


def _wait_until(check, seconds):
    deadline = time.monotonic() + seconds
    while True:
        if check():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(2.0)


@contextlib.contextmanager
def _single_run_lock():
    import fcntl

    tools = _tools_dir()
    tools.mkdir(parents=True, exist_ok=True)
    handle = (tools / "start_prod.lock").open("a+", encoding="utf-8")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise LauncherError("Скрипт уже выполняется в другом окне: дождитесь его завершения.") from exc
        yield
    finally:
        handle.close()


def _check_host():
    if sys.platform != "darwin":
        raise LauncherError("Скрипт рассчитан на macOS: нужны Docker Desktop и launchctl.")
    if os.geteuid() == 0:
        raise LauncherError("Запускайте скрипт от своего пользователя, без sudo.")


def _require_tools():
    if not _which("docker"):
        raise LauncherError("Не найден docker. Установите Docker Desktop и повторите запуск.")
    ngrok = _which("ngrok")
    if not ngrok:
        raise LauncherError("Не найден ngrok. Установите его и настройте авторизацию: docs/LOCAL_DEMO.md.")
    return ngrok


def _require_env_file():
    env_file = _env_file()
    if not env_file.is_file() or env_file.stat().st_size == 0:
        raise LauncherError(
            "Нет заполненного .env рядом с docker-compose.yml. Создайте его по docs/LOCAL_DEMO.md: "
            "скрипт не создаёт и не меняет секреты."
        )
    return env_file


def _ensure_docker():
    if _run(["docker", "info"], timeout=30).returncode == 0:
        return
    print("Docker не отвечает, открываю Docker Desktop.", flush=True)
    _run(["open", "-a", "Docker"], timeout=30)
    if not _wait_until(lambda: _run(["docker", "info"], timeout=30).returncode == 0, DOCKER_WAIT_SECONDS):
        raise LauncherError(
            f"Docker Desktop не ответил за {DOCKER_WAIT_SECONDS} с. Запустите его вручную и повторите команду."
        )


def _compose(project, compose_file, env_file=None):
    command = ["docker", "compose", "-p", project, "-f", str(compose_file)]
    if env_file is not None:
        command += ["--env-file", str(env_file)]
    return command


def _compose_config_quiet(project, compose_file, env_file=None):
    result = _run([*_compose(project, compose_file, env_file), "config", "--quiet"], timeout=60)
    if result.returncode != 0:
        raise LauncherError(f"Конфигурация {compose_file} не собирается: {result.stderr.strip()}")


def _resolved_ports(project, compose_file, env_file, service):
    result = _run([*_compose(project, compose_file, env_file), "config", "--format", "json"], timeout=60)
    if result.returncode != 0:
        raise LauncherError(f"Не удалось разрешить конфигурацию {compose_file}: {result.stderr.strip()}")
    try:
        resolved = json.loads(result.stdout)
    except ValueError as exc:
        raise LauncherError(f"Не удалось разобрать конфигурацию {compose_file}.") from exc
    ports = (resolved.get("services") or {}).get(service, {}).get("ports") or []
    return [(str(port.get("host_ip") or ""), str(port.get("published") or "")) for port in ports]


def _assert_published(project, compose_file, env_file, service, expected_port, role):
    binds = _resolved_ports(project, compose_file, env_file, service)
    if binds != [("127.0.0.1", expected_port)]:
        raise LauncherError(
            f"{role}: ожидается единственная публикация 127.0.0.1:{expected_port}, получено {binds}. "
            "Исправьте .env сами: скрипт его не меняет."
        )


def _assert_ports(env_file):
    _assert_published(MAIN_PROJECT, MAIN_COMPOSE, env_file, "api", EXPECTED_API_PORT, "API")
    _assert_published(GATEWAY_PROJECT, GATEWAY_COMPOSE, None, "nginx", EXPECTED_GATEWAY_PORT, "Шлюз")


def _job_state(label):
    """None — job не загружен, 0 — загружен без процесса, иначе pid процесса."""
    result = _run(["launchctl", "print", f"gui/{_uid()}/{label}"], timeout=15)
    if result.returncode != 0:
        return None
    match = re.search(r"\bpid = (\d+)", result.stdout or "")
    return int(match.group(1)) if match else 0


def _launchctl(*arguments):
    result = _run(["launchctl", *arguments], timeout=30)
    if result.returncode != 0:
        raise LauncherError(f"launchctl {arguments[0]} не выполнена: {result.stderr.strip()}")


def _replace_placeholders(value, replacements):
    if isinstance(value, str):
        for placeholder, replacement in replacements.items():
            value = value.replace(placeholder, replacement)
        return value
    if isinstance(value, list):
        return [_replace_placeholders(item, replacements) for item in value]
    if isinstance(value, dict):
        return {key: _replace_placeholders(item, replacements) for key, item in value.items()}
    return value


def _render_plist(ngrok_bin):
    if not PLIST_TEMPLATE.is_file():
        raise LauncherError(f"Нет шаблона LaunchAgent: {PLIST_TEMPLATE}")
    try:
        template = plistlib.loads(PLIST_TEMPLATE.read_bytes())
    except ValueError as exc:
        raise LauncherError(f"Шаблон LaunchAgent не разбирается: {exc}") from exc
    # Подстановка идёт по строковым значениям разобранного plist: XML-экранирование
    # делает plistlib при записи, поэтому символы вроде & в путях безопасны.
    plist = _replace_placeholders(template, {"__HOME__": str(_home()), "__NGROK_BIN__": str(ngrok_bin)})
    if plist.get("Label") != NEW_LABEL:
        raise LauncherError(f"В шаблоне LaunchAgent ожидается Label {NEW_LABEL}.")
    return plist


def _read_plist(path):
    try:
        with path.open("rb") as handle:
            return plistlib.load(handle)
    except (FileNotFoundError, OSError, ValueError):
        return None


def _write_plist(plist):
    path = _plist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    _log_path().parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        plistlib.dump(plist, handle)
    return path


def _prepare_launch_agent():
    _log_path().parent.mkdir(parents=True, exist_ok=True)
    # enable нужен, чтобы поднять службу, ранее выключенную через disable.
    _launchctl("enable", f"gui/{_uid()}/{NEW_LABEL}")


def _disable_legacy(legacy_state):
    if legacy_state is None:
        return
    _launchctl("disable", f"gui/{_uid()}/{LEGACY_NGROK_LABEL}")
    _launchctl("bootout", f"gui/{_uid()}/{LEGACY_NGROK_LABEL}")


def _ensure_ngrok(desired_plist, tunnels):
    new_state = _job_state(NEW_LABEL)
    legacy_state = _job_state(LEGACY_NGROK_LABEL)
    # Агент на 4040 считается своим только при запущенном процессе известной службы:
    # загруженная, но упавшая служба не объясняет занятый порт.
    running_owner = (new_state or 0) > 0 or (legacy_state or 0) > 0
    if tunnels is not None and not running_owner:
        raise LauncherError(
            "Порт 4040 занят агентом ngrok, который не относится к запущенным службам. "
            "Остановите его сами: скрипт не завершает чужие процессы."
        )
    # Установленная копия сравнивается разобранным plist: комментарии шаблона не важны.
    installed = _read_plist(_plist_path())
    if installed == desired_plist:
        if new_state == 0:
            # Служба загружена без процесса (например, после disable): включаем заново.
            _prepare_launch_agent()
            _launchctl("bootout", f"gui/{_uid()}/{NEW_LABEL}")
            _launchctl("bootstrap", f"gui/{_uid()}", str(_plist_path()))
        elif new_state is None:
            _prepare_launch_agent()
            _disable_legacy(legacy_state)
            _launchctl("bootstrap", f"gui/{_uid()}", str(_plist_path()))
        return
    # Сначала освобождаем 4040 от старой службы, чтобы два агента не оказались живыми.
    _disable_legacy(legacy_state)
    if new_state is not None:
        _launchctl("bootout", f"gui/{_uid()}/{NEW_LABEL}")
    _write_plist(desired_plist)
    _prepare_launch_agent()
    _launchctl("bootstrap", f"gui/{_uid()}", str(_plist_path()))


def _reset_demo_data(env_file):
    # Удаляются только тома проекта upivo-demo: общий шлюз, ngrok и другие проекты не затрагиваются.
    print(
        f"ВНИМАНИЕ: --reset-db удаляет данные PostgreSQL и Redis демостенда {MAIN_PROJECT}. "
        "Резервная копия не создаётся; общий шлюз, ngrok и другое окружение не меняются.",
        file=sys.stderr,
        flush=True,
    )
    command = [*_compose(MAIN_PROJECT, MAIN_COMPOSE, env_file), "--profile", "workers", "down", "--volumes"]
    result = _run(command, timeout=300)
    if result.returncode != 0:
        raise LauncherError(f"Не удалось удалить данные {MAIN_PROJECT} (--reset-db): {result.stderr.strip()}")


def _start_stack(env_file, *, no_build):
    main = [*_compose(MAIN_PROJECT, MAIN_COMPOSE, env_file), "--profile", "workers", "up", "-d"]
    if not no_build:
        main.append("--build")
    main += ["--wait", "--wait-timeout", "120"]
    result = _run(main, timeout=900)
    if result.returncode != 0:
        raise LauncherError(f"Сборка или запуск {MAIN_PROJECT} не удались: {result.stderr.strip()}")
    result = _run([*_compose(GATEWAY_PROJECT, GATEWAY_COMPOSE), "up", "-d"], timeout=180)
    if result.returncode != 0:
        raise LauncherError(f"Запуск {GATEWAY_PROJECT} не удался: {result.stderr.strip()}")


def _wait_local_gateway():
    for path in ("/upivo/health", "/upivo/health/db", "/upivo/docs"):
        url = LOCAL_GATEWAY + path
        if not _wait_until(lambda target=url: _http_status(target) == 200, READY_SECONDS):
            raise LauncherError(
                f"Локальная проверка не прошла: {url}. Контейнеры оставлены как есть; "
                "смотрите docker compose ps и журналы."
            )


def _load_tunnels():
    payload = _json(NGROK_TUNNELS)
    if not isinstance(payload, dict):
        return None
    tunnels = payload.get("tunnels")
    return tunnels if isinstance(tunnels, list) else None


def _tunnel_address(tunnel):
    return str((tunnel.get("config") or {}).get("addr") or "").strip().lower().rstrip("/")


def _wait_gateway_tunnel():
    deadline = time.monotonic() + READY_SECONDS
    while True:
        tunnels = _load_tunnels() or []
        for tunnel in tunnels:
            if _tunnel_address(tunnel) != GATEWAY_UPSTREAM:
                raise LauncherError(
                    f"Агент ngrok направляет другой upstream ({_tunnel_address(tunnel) or 'неизвестно'}). "
                    "Скрипт не перенаправляет и не удаляет чужие эндпоинты."
                )
            if (tunnel.get("config") or {}).get("inspect") is not False:
                raise LauncherError(
                    "Туннель ngrok включает inspect и может записывать данные пользователей. "
                    "Перезапустите службу с --inspect=false: скрипт не меняет настройки чужого туннеля."
                )
        public = [tunnel for tunnel in tunnels if str(tunnel.get("public_url") or "").startswith("https://")]
        if len(public) == 1:
            return str(public[0]["public_url"])[len("https://") :].rstrip("/")
        if time.monotonic() >= deadline:
            raise LauncherError(
                "Туннель ngrok на порт 18080 не появился. Локальный Swagger: "
                f"{LOCAL_GATEWAY}/upivo/docs; журнал: {_log_path()}."
            )
        time.sleep(2.0)


def _wait_public(domain):
    for path in ("/upivo/health", "/upivo/docs"):
        url = f"https://{domain}{path}"
        if not _wait_until(lambda target=url: _http_status(target, headers=PUBLIC_HEADERS) == 200, READY_SECONDS):
            raise LauncherError(
                f"Публичная проверка не прошла: {url}. Локальный Swagger: {LOCAL_GATEWAY}/upivo/docs; "
                f"журнал ngrok: {_log_path()}. Контейнеры и данные оставлены как есть."
            )


def _report_legacy():
    if _http_status(LOCAL_GATEWAY + "/healthz") != 200:
        print(
            "Внимание: сервис на 8765 не отвечает через шлюз. Скрипт его не устанавливает и не перезапускает.",
            file=sys.stderr,
            flush=True,
        )


def main(argv=None):
    parser = argparse.ArgumentParser(description="Запуск демостенда uPIVO одной командой.")
    parser.add_argument("--no-build", action="store_true", help="не пересобирать образ API")
    parser.add_argument(
        "--reset-db",
        action="store_true",
        help="удалить данные PostgreSQL и Redis демостенда upivo-demo перед запуском (необратимо)",
    )
    arguments = parser.parse_args(argv)
    try:
        _check_host()
        ngrok_bin = _require_tools()
        env_file = _require_env_file()
        desired_plist = _render_plist(ngrok_bin)
        with _single_run_lock():
            _ensure_docker()
            _compose_config_quiet(MAIN_PROJECT, MAIN_COMPOSE, env_file)
            _compose_config_quiet(GATEWAY_PROJECT, GATEWAY_COMPOSE)
            _assert_ports(env_file)
            if arguments.reset_db:
                _reset_demo_data(env_file)
            # Сначала локальная готовность: до неё туннель и старая служба не меняются.
            _start_stack(env_file, no_build=arguments.no_build)
            _wait_local_gateway()
            _ensure_ngrok(desired_plist, _load_tunnels())
            domain = _wait_gateway_tunnel()
            _wait_public(domain)
            _report_legacy()
            print(f"Локальный Swagger: {LOCAL_GATEWAY}/upivo/docs", flush=True)
            print(f"Публичный Swagger: https://{domain}/upivo/docs", flush=True)
    except LauncherError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
