"""Контракты единой команды запуска демостенда.

Тесты не запускают Docker, launchctl и не ходят в сеть: подпроцессы, HTTP,
домашний каталог и время подменяются.
"""

import io
import json
import os
import plistlib
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import start_prod  # noqa: E402

DOCKER = "/opt/homebrew/bin/docker"
NGROK = "/opt/homebrew/bin/ngrok"
UID = 501

MAIN_CONFIG = {
    "name": "upivo-demo",
    "services": {
        "api": {"ports": [{"host_ip": "127.0.0.1", "published": "18000", "target": 8000}]},
    },
}
GATEWAY_CONFIG = {
    "name": "upivo-gateway",
    "services": {
        "nginx": {"ports": [{"host_ip": "127.0.0.1", "published": "18080", "target": 8080}]},
    },
}
GATEWAY_TUNNELS = {
    "tunnels": [
        {"public_url": "https://demo.ngrok-free.app", "config": {"addr": "http://127.0.0.1:18080", "inspect": False}},
        {"public_url": "http://demo.ngrok-free.app", "config": {"addr": "http://127.0.0.1:18080", "inspect": False}},
    ]
}


class Recorder:
    """Заменяет выполнение команд: запоминает вызовы и отдаёт заданные коды."""

    def __init__(self, handler):
        self.commands = []
        self._handler = handler

    def __call__(self, command, *, timeout=60, cwd=None):
        parts = [str(part) for part in command]
        self.commands.append(parts)
        code, out = self._handler(parts)
        return subprocess.CompletedProcess(parts, code, out, "")

    def has(self, *parts):
        return any(all(part in command for part in parts) for command in self.commands)

    def joined(self):
        return [" ".join(command) for command in self.commands]


class FakeTime:
    """Быстрое время: любой ограниченный ожидающий цикл завершается сразу."""

    def __init__(self):
        self.value = 0.0

    def monotonic(self):
        self.value += 1000.0
        return self.value

    def sleep(self, _seconds):
        return None


class LauncherTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name) / "home"
        (self.home / "Library" / "LaunchAgents").mkdir(parents=True)
        self.env_file = Path(temporary.name) / ".env"
        self.env_file.write_text("POSTGRES_PASSWORD=fixture\n", encoding="utf-8")
        self.tools = Path(temporary.name) / "tools"

        for patcher in (
            mock.patch.object(start_prod, "_home", return_value=self.home),
            mock.patch.object(start_prod, "_env_file", return_value=self.env_file),
            mock.patch.object(start_prod, "_tools_dir", return_value=self.tools),
            mock.patch.object(start_prod, "_uid", return_value=UID),
            mock.patch.object(start_prod, "_which", side_effect=lambda name: {"docker": DOCKER, "ngrok": NGROK}.get(name)),
            mock.patch.object(start_prod, "time", FakeTime()),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

        self.statuses = {}
        status_patch = mock.patch.object(start_prod, "_http_status", side_effect=self._status)
        status_patch.start()
        self.addCleanup(status_patch.stop)

    def _status(self, url, headers=None, timeout=3.0):
        self.statuses[url] = True
        return 200

    def desired_plist(self):
        text = start_prod.PLIST_TEMPLATE.read_text(encoding="utf-8")
        return plistlib.loads(text.replace("__HOME__", str(self.home)).replace("__NGROK_BIN__", NGROK).encode("utf-8"))

    def install_plist(self, plist=None):
        target = self.home / "Library" / "LaunchAgents" / f"{start_prod.NEW_LABEL}.plist"
        with target.open("wb") as handle:
            plistlib.dump(plist if plist is not None else self.desired_plist(), handle)
        return target

    def default_handler(self, parts):
        if parts[:2] == ["docker", "info"]:
            return 0, ""
        if parts[:2] == ["launchctl", "print"]:
            return (0, "pid = 4242\n") if start_prod.NEW_LABEL in parts[-1] else (1, "")
        if parts[:3] == ["docker", "compose", "-p"] and "config" in parts:
            if "--format" in parts:
                return 0, json.dumps(MAIN_CONFIG if "upivo-demo" in parts else GATEWAY_CONFIG)
            return 0, ""
        return 0, ""

    def run_launcher(self, handler=None, argv=None, tunnels=None):
        recorder = Recorder(handler or self.default_handler)
        answers = mock.Mock(side_effect=tunnels) if callable(tunnels) else mock.Mock(return_value=tunnels)
        with mock.patch.object(start_prod, "_run", recorder):
            with mock.patch.object(start_prod, "_json", answers):
                out, err = io.StringIO(), io.StringIO()
                with redirect_stdout(out), redirect_stderr(err):
                    code = start_prod.main(argv or [])
        return code, out.getvalue(), err.getvalue(), recorder

    def test_docker_start_and_wait_timeout(self):
        def handler(parts):
            if parts[:2] == ["docker", "info"]:
                return 1, ""
            return self.default_handler(parts)

        code, _out, err, recorder = self.run_launcher(handler=handler)

        self.assertEqual(code, 1)
        self.assertIn("120", err)
        self.assertTrue(recorder.has("open", "-a", "Docker"))
        self.assertFalse(recorder.has("docker", "compose", "up"))
        self.assertFalse(recorder.has("down"))

    def test_compose_commands_scoped_to_projects_and_workers(self):
        self.install_plist()

        code, out, err, recorder = self.run_launcher(tunnels=GATEWAY_TUNNELS)

        self.assertEqual(code, 0, err)
        main_up = [command for command in recorder.commands if "up" in command and start_prod.MAIN_PROJECT in command]
        self.assertEqual(len(main_up), 1, recorder.joined())
        command = main_up[0]
        self.assertIn(str(start_prod.MAIN_COMPOSE), command)
        self.assertIn(str(self.env_file), command)
        self.assertIn("--profile", command)
        self.assertIn("workers", command)
        self.assertIn("--wait", command)
        self.assertIn("--build", command)
        gateway_up = [item for item in recorder.commands if "up" in item and start_prod.GATEWAY_PROJECT in item]
        self.assertEqual(len(gateway_up), 1, recorder.joined())
        self.assertIn(str(start_prod.GATEWAY_COMPOSE), gateway_up[0])
        self.assertNotIn("--profile", gateway_up[0])
        for forbidden in ("down", "-v", "rm", "stop"):
            self.assertFalse(any(forbidden in command for command in recorder.commands), forbidden)
        self.assertIn("Локальный Swagger: http://127.0.0.1:18080/upivo/docs", out)
        self.assertIn("Публичный Swagger: https://demo.ngrok-free.app/upivo/docs", out)

    def test_running_ngrok_job_is_reused_without_restart(self):
        self.install_plist()

        code, _out, err, recorder = self.run_launcher(tunnels=GATEWAY_TUNNELS)

        self.assertEqual(code, 0, err)
        launchctl = [command for command in recorder.commands if command[0] == "launchctl"]
        self.assertTrue(launchctl)
        self.assertFalse([command for command in launchctl if command[1] != "print"], launchctl)
        self.assertFalse(any("kickstart" in command for command in recorder.commands))
        mutating = [command for command in recorder.commands if command[0] == "launchctl" and command[1] != "print"]
        self.assertFalse(any("local.doska-chatgpt" in part for command in mutating for part in command))

    def test_legacy_ngrok_disabled_only_when_loaded(self):
        self.install_plist()
        self.install_plist(plist={"Label": "stale"})

        def handler(parts):
            if parts[:2] == ["launchctl", "print"]:
                if start_prod.LEGACY_NGROK_LABEL in parts[-1]:
                    return 0, "pid = 5151\n"
                return 1, ""
            return self.default_handler(parts)

        answers = [None, GATEWAY_TUNNELS]
        code, _out, err, recorder = self.run_launcher(
            handler=handler, tunnels=lambda *_args: answers.pop(0) if len(answers) > 1 else GATEWAY_TUNNELS
        )

        self.assertEqual(code, 0, err)
        joined = recorder.joined()
        disable = [index for index, line in enumerate(joined) if "launchctl disable" in line]
        legacy_bootout = [index for index, line in enumerate(joined) if "launchctl bootout" in line]
        bootstrap = [index for index, line in enumerate(joined) if "launchctl bootstrap" in line]
        self.assertTrue(disable and legacy_bootout and bootstrap, joined)
        self.assertLess(disable[0], legacy_bootout[0])
        self.assertLess(legacy_bootout[0], bootstrap[0])
        self.assertIn(start_prod.LEGACY_NGROK_LABEL, joined[disable[0]])
        self.assertIn(start_prod.LEGACY_NGROK_LABEL, joined[legacy_bootout[0]])
        self.assertIn(f"gui/{UID}", joined[bootstrap[0]])
        self.assertFalse(any("local.doska-chatgpt.bridge" in line for line in joined))

    def test_foreign_upstream_is_rejected_without_teardown(self):
        self.install_plist()
        foreign = {"tunnels": [{"public_url": "https://demo.ngrok-free.app", "config": {"addr": "http://127.0.0.1:8765"}}]}

        code, _out, err, recorder = self.run_launcher(tunnels=foreign)

        self.assertNotEqual(code, 0)
        self.assertIn("upstream", err)
        self.assertFalse(any("down" in command for command in recorder.commands))
        self.assertFalse(recorder.has("kickstart"))

    def test_public_check_failure_returns_error_and_keeps_stack(self):
        self.install_plist()

        def status(url, headers=None, timeout=3.0):
            return 200 if url.startswith(start_prod.LOCAL_GATEWAY) else None

        with mock.patch.object(start_prod, "_http_status", side_effect=status):
            code, _out, err, recorder = self.run_launcher(tunnels=GATEWAY_TUNNELS)

        self.assertEqual(code, 1)
        self.assertIn("http://127.0.0.1:18080/upivo/docs", err)
        self.assertIn("ngrok.log", err)
        self.assertFalse(any("down" in command for command in recorder.commands))

    def test_missing_env_file_stops_before_docker(self):
        self.env_file.unlink()

        code, _out, err, recorder = self.run_launcher(tunnels=GATEWAY_TUNNELS)

        self.assertEqual(code, 1)
        self.assertIn(".env", err)
        self.assertEqual(recorder.commands, [])

    def test_wrong_api_port_is_rejected(self):
        self.install_plist()
        broken = json.loads(json.dumps(MAIN_CONFIG))
        broken["services"]["api"]["ports"][0]["published"] = "8000"

        def handler(parts):
            if "--format" in parts:
                return 0, json.dumps(broken)
            return self.default_handler(parts)

        code, _out, err, recorder = self.run_launcher(handler=handler, tunnels=GATEWAY_TUNNELS)

        self.assertEqual(code, 1)
        self.assertIn("18000", err)
        self.assertFalse(recorder.has("docker", "compose", "up"))

    def test_loaded_but_stopped_job_does_not_excuse_foreign_agent(self):
        self.install_plist()

        def handler(parts):
            if parts[:2] == ["launchctl", "print"]:
                if start_prod.NEW_LABEL in parts[-1]:
                    return 0, "state = not running\n"
                return 1, ""
            return self.default_handler(parts)

        code, _out, err, recorder = self.run_launcher(handler=handler, tunnels=GATEWAY_TUNNELS)

        self.assertEqual(code, 1)
        self.assertIn("4040", err)
        self.assertFalse(any("kickstart" in command for command in recorder.commands))
        self.assertFalse(any("bootstrap" in command for command in recorder.commands))
        self.assertFalse(any("down" in command for command in recorder.commands))


    def test_local_readiness_failure_skips_ngrok_mutation(self):
        def status(url, headers=None, timeout=3.0):
            return 200 if url.endswith("/healthz") else None

        with mock.patch.object(start_prod, "_http_status", side_effect=status):
            code, _out, err, recorder = self.run_launcher(tunnels=None)

        self.assertEqual(code, 1)
        self.assertIn("Локальная проверка", err)
        self.assertTrue(recorder.has("docker", "compose", "up"))
        mutations = [command for command in recorder.commands if command[0] == "launchctl" and command[1] != "print"]
        self.assertEqual(mutations, [], recorder.joined())
        self.assertFalse(any("down" in command for command in recorder.commands))

    def test_ngrok_bootstrap_runs_after_stack_and_local_checks(self):
        answers = [None, GATEWAY_TUNNELS]
        code, _out, err, recorder = self.run_launcher(
            tunnels=lambda *_args: answers.pop(0) if len(answers) > 1 else GATEWAY_TUNNELS
        )

        self.assertEqual(code, 0, err)
        joined = recorder.joined()
        main_up = next(index for index, line in enumerate(joined) if "up -d" in line and start_prod.MAIN_PROJECT in line)
        gateway_up = next(index for index, line in enumerate(joined) if "up -d" in line and start_prod.GATEWAY_PROJECT in line)
        bootstrap = next(index for index, line in enumerate(joined) if "launchctl bootstrap" in line)
        self.assertGreater(bootstrap, main_up)
        self.assertGreater(bootstrap, gateway_up)

    def test_disabled_matching_plist_is_enabled_then_bootstrapped(self):
        self.install_plist()
        log_dir = self.home / "Library" / "Logs" / "upivo-gateway"
        self.assertFalse(log_dir.exists())
        answers = [None, GATEWAY_TUNNELS]

        def handler(parts):
            if parts[:2] == ["launchctl", "print"]:
                if start_prod.NEW_LABEL in parts[-1]:
                    return 0, "state = not running\n"
                return 1, ""
            return self.default_handler(parts)

        code, _out, err, recorder = self.run_launcher(
            handler=handler, tunnels=lambda *_args: answers.pop(0) if len(answers) > 1 else GATEWAY_TUNNELS
        )

        self.assertEqual(code, 0, err)
        joined = recorder.joined()
        enable = [index for index, line in enumerate(joined) if "launchctl enable" in line]
        bootout = [index for index, line in enumerate(joined) if "launchctl bootout" in line]
        bootstrap = [index for index, line in enumerate(joined) if "launchctl bootstrap" in line]
        self.assertEqual(len(enable), 1, joined)
        self.assertEqual(len(bootout), 1, joined)
        self.assertEqual(len(bootstrap), 1, joined)
        self.assertIn(f"gui/{UID}/{start_prod.NEW_LABEL}", joined[enable[0]])
        self.assertLess(enable[0], bootout[0])
        self.assertLess(bootout[0], bootstrap[0])
        self.assertTrue(log_dir.is_dir())

    def test_installed_plist_absent_is_enabled_then_bootstrapped(self):
        log_dir = self.home / "Library" / "Logs" / "upivo-gateway"
        answers = [None, GATEWAY_TUNNELS]

        code, _out, err, recorder = self.run_launcher(
            tunnels=lambda *_args: answers.pop(0) if len(answers) > 1 else GATEWAY_TUNNELS
        )

        self.assertEqual(code, 0, err)
        joined = recorder.joined()
        enable = [index for index, line in enumerate(joined) if "launchctl enable" in line]
        bootstrap = [index for index, line in enumerate(joined) if "launchctl bootstrap" in line]
        self.assertEqual(len(enable), 1, joined)
        self.assertEqual(len(bootstrap), 1, joined)
        self.assertLess(enable[0], bootstrap[0])
        self.assertTrue((self.home / "Library" / "LaunchAgents" / f"{start_prod.NEW_LABEL}.plist").is_file())
        self.assertTrue(log_dir.is_dir())

    def test_inspected_tunnel_is_rejected(self):
        self.install_plist()
        inspected = {
            "tunnels": [
                {
                    "public_url": "https://demo.ngrok-free.app",
                    "config": {"addr": "http://127.0.0.1:18080", "inspect": True},
                }
            ]
        }

        code, _out, err, recorder = self.run_launcher(tunnels=inspected)

        self.assertEqual(code, 1)
        self.assertIn("inspect", err)
        self.assertFalse(any("down" in command for command in recorder.commands))
        self.assertFalse(recorder.has("kickstart"))

    def test_home_with_ampersand_stays_escaped(self):
        amp_home = Path(str(self.home) + "&co")
        with mock.patch.object(start_prod, "_home", return_value=amp_home):
            plist = start_prod._render_plist(NGROK)

        self.assertEqual(plist["ProgramArguments"][0], NGROK)
        self.assertEqual(plist["EnvironmentVariables"]["HOME"], str(amp_home))
        self.assertEqual(plist["StandardOutPath"], str(amp_home / "Library" / "Logs" / "upivo-gateway" / "ngrok.log"))
        self.assertEqual(plistlib.loads(plistlib.dumps(plist)), plist)

    def test_default_run_does_not_delete_data(self):
        self.install_plist()

        code, out, err, recorder = self.run_launcher(tunnels=GATEWAY_TUNNELS)

        self.assertEqual(code, 0, err)
        self.assertFalse(recorder.has("down"))
        self.assertFalse(recorder.has("--volumes"))
        self.assertNotIn("--reset-db", out + err)

    def test_reset_runs_after_checks_and_before_stack(self):
        self.install_plist()

        code, out, err, recorder = self.run_launcher(argv=["--reset-db"], tunnels=GATEWAY_TUNNELS)

        self.assertEqual(code, 0, err)
        joined = recorder.joined()
        info = next(index for index, line in enumerate(joined) if line.startswith("docker info"))
        checks = [index for index, line in enumerate(joined) if " config " in line]
        down = next(index for index, line in enumerate(joined) if "down --volumes" in line)
        main_up = next(index for index, line in enumerate(joined) if "up -d" in line and start_prod.MAIN_PROJECT in line)
        self.assertTrue(checks, joined)
        self.assertLess(info, down)
        self.assertLess(max(checks), down)
        self.assertLess(down, main_up)
        self.assertIn("PostgreSQL", err)
        self.assertIn("Redis", err)

    def test_reset_down_is_scoped_to_demo_project(self):
        self.install_plist()

        code, _out, err, recorder = self.run_launcher(argv=["--reset-db"], tunnels=GATEWAY_TUNNELS)

        self.assertEqual(code, 0, err)
        downs = [command for command in recorder.commands if "down" in command]
        self.assertEqual(len(downs), 1, recorder.joined())
        self.assertEqual(
            downs[0],
            [
                "docker",
                "compose",
                "-p",
                start_prod.MAIN_PROJECT,
                "-f",
                str(start_prod.MAIN_COMPOSE),
                "--env-file",
                str(self.env_file),
                "--profile",
                "workers",
                "down",
                "--volumes",
            ],
        )
        self.assertNotIn(start_prod.GATEWAY_PROJECT, downs[0])
        self.assertFalse(any("prune" in part for command in recorder.commands for part in command))
        self.assertFalse(recorder.has("volume", "rm"))

    def test_reset_warning_is_printed_before_down(self):
        self.install_plist()
        buffer = io.StringIO()
        seen = []

        def handler(parts):
            seen.append((parts, buffer.getvalue()))
            return self.default_handler(parts)

        with mock.patch.object(start_prod, "_run", Recorder(handler)):
            with mock.patch.object(start_prod, "_json", mock.Mock(return_value=GATEWAY_TUNNELS)):
                with redirect_stdout(buffer), redirect_stderr(buffer):
                    code = start_prod.main(["--reset-db"])

        self.assertEqual(code, 0, buffer.getvalue())
        _down, before_down = next((parts, text) for parts, text in seen if "down" in parts)
        self.assertIn("PostgreSQL", before_down)
        self.assertIn("Redis", before_down)

    def test_failed_reset_stops_before_stack_and_ngrok(self):
        self.install_plist()

        def handler(parts):
            if "down" in parts:
                return 1, ""
            return self.default_handler(parts)

        code, _out, err, recorder = self.run_launcher(handler=handler, argv=["--reset-db"], tunnels=GATEWAY_TUNNELS)

        self.assertEqual(code, 1)
        self.assertIn("Ошибка", err)
        self.assertIn(start_prod.MAIN_PROJECT, err)
        self.assertTrue(recorder.has("docker", "compose", "down"))
        self.assertFalse(recorder.has("docker", "compose", "up"))
        mutations = [command for command in recorder.commands if command[0] == "launchctl" and command[1] != "print"]
        self.assertEqual(mutations, [], recorder.joined())
        self.assertFalse(any("kickstart" in command for command in recorder.commands))

    def test_reset_combines_with_no_build(self):
        self.install_plist()

        code, _out, err, recorder = self.run_launcher(argv=["--reset-db", "--no-build"], tunnels=GATEWAY_TUNNELS)

        self.assertEqual(code, 0, err)
        joined = recorder.joined()
        down = next(index for index, line in enumerate(joined) if "down --volumes" in line)
        main_up = next(index for index, line in enumerate(joined) if "up -d" in line and start_prod.MAIN_PROJECT in line)
        self.assertLess(down, main_up)
        self.assertNotIn("--build", recorder.commands[main_up])

    def test_import_has_no_side_effects(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary)
            (target / "home").mkdir()
            source = ROOT / "start_prod.py"
            (target / "start_prod.py").write_bytes(source.read_bytes())
            environment = dict(os.environ, HOME=str(target / "home"), PYTHONPATH="")
            result = subprocess.run(
                [sys.executable, "-c", "import start_prod"],
                cwd=target,
                env=environment,
                capture_output=True,
                text=True,
                timeout=60,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "")
            self.assertFalse((target / ".env").exists())
            self.assertFalse((target / ".tools").exists())
            self.assertFalse((target / "home" / "Library").exists())


if __name__ == "__main__":
    unittest.main()
