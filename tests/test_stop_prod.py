"""Контракты остановки демостенда.

Тесты не запускают Docker и не останавливают стенд: выполнение команд подменяется.
"""

import io
import os
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
import stop_prod  # noqa: E402

SUCCESS = "Демостенд upivo-demo остановлен. Данные, общий шлюз и ngrok сохранены."


class Recorder:
    """Заменяет выполнение команд start_prod: запоминает вызовы и отдаёт заданный код."""

    def __init__(self, *, code=0, out="", err="", error=None):
        self.code = code
        self.out = out
        self.err = err
        self.error = error
        self.commands = []
        self.timeouts = []

    def __call__(self, command, *, timeout=60, cwd=None):
        parts = [str(part) for part in command]
        self.commands.append(parts)
        self.timeouts.append(timeout)
        if self.error is not None:
            raise self.error
        return subprocess.CompletedProcess(parts, self.code, self.out, self.err)


class StopTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.env_file = Path(temporary.name) / ".env"
        self.env_file.write_text("POSTGRES_PASSWORD=fixture\n", encoding="utf-8")
        self.tools = Path(temporary.name) / "tools"

        for patcher in (
            mock.patch.object(start_prod, "_env_file", return_value=self.env_file),
            mock.patch.object(start_prod, "_tools_dir", return_value=self.tools),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def expected_command(self):
        return [
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
        ]

    def run_stop(self, recorder=None, argv=None):
        recorder = recorder or Recorder()
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(start_prod, "_run", recorder):
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = stop_prod.main(argv if argv is not None else [])
        return code, stdout.getvalue(), stderr.getvalue(), recorder

    def test_down_is_scoped_to_demo_project_without_data_loss(self):
        code, out, err, recorder = self.run_stop()

        self.assertEqual(code, 0, err)
        self.assertEqual(len(recorder.commands), 1, recorder.commands)
        command = recorder.commands[0]
        self.assertEqual(command, self.expected_command())
        self.assertEqual(recorder.timeouts, [300])
        self.assertIn(str(start_prod.MAIN_COMPOSE), command)
        self.assertIn(str(self.env_file), command)
        self.assertNotIn(start_prod.GATEWAY_PROJECT, command)
        for forbidden in (
            "--volumes",
            "-v",
            "--rmi",
            "--remove-orphans",
            "prune",
            "up",
            "restart",
            "stop",
            "rm",
            "gateway",
            "launchctl",
            "kickstart",
            "info",
            "open",
        ):
            self.assertNotIn(forbidden, command)
        self.assertEqual(out.strip(), SUCCESS)

    def test_running_start_prod_blocks_stop(self):
        with start_prod._single_run_lock():
            code, out, err, recorder = self.run_stop()

        self.assertEqual(code, 1, err)
        self.assertEqual(recorder.commands, [])
        self.assertIn("другом окне", err)
        self.assertNotIn(SUCCESS, out)

    def test_failed_down_never_reports_success(self):
        secret = 'FATAL: password authentication failed for user "upivo_demo"'
        recorder = Recorder(code=1, err=secret)

        code, out, err, _recorder = self.run_stop(recorder)

        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("Ошибка", err)
        self.assertIn(start_prod.MAIN_PROJECT, err)
        self.assertNotIn("FATAL", err)
        self.assertNotIn("password", err)

    def test_timeout_or_stopped_project_is_handled(self):
        code, out, err, _recorder = self.run_stop(Recorder(code=124, err="таймаут 300 с"))
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertNotIn(SUCCESS, out + err)

        code, out, err, _recorder = self.run_stop(Recorder(code=0, err=""))
        self.assertEqual(code, 0, err)
        self.assertEqual(out.strip(), SUCCESS)

    def test_missing_env_file_stops_before_docker(self):
        self.env_file.unlink()
        recorder = Recorder()

        code, out, err, recorder = self.run_stop(recorder)

        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn(".env", err)
        self.assertEqual(recorder.commands, [])

    def test_os_error_is_reported_without_traceback_details(self):
        recorder = Recorder(error=OSError("[Errno 2] No such file or directory: 'docker'"))

        code, out, err, _recorder = self.run_stop(recorder)

        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("Ошибка", err)
        self.assertNotIn("Errno", err)
        self.assertNotIn("Traceback", err)
        self.assertNotIn(SUCCESS, out)

    def test_help_has_no_side_effects(self):
        recorder = Recorder()
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(start_prod, "_run", recorder):
            with mock.patch.object(start_prod, "_require_env_file", mock.Mock()) as require_env:
                with mock.patch.object(start_prod, "_single_run_lock", mock.Mock()) as lock:
                    with redirect_stdout(stdout), redirect_stderr(stderr):
                        with self.assertRaises(SystemExit) as raised:
                            stop_prod.main(["--help"])

        self.assertEqual(raised.exception.code, 0)
        self.assertIn("usage:", stdout.getvalue())
        self.assertEqual(recorder.commands, [])
        self.assertEqual(require_env.call_count, 0)
        self.assertEqual(lock.call_count, 0)
        self.assertFalse(self.tools.exists())

    def test_destructive_options_do_not_exist(self):
        for argv in (["--reset-db"], ["--volumes"], ["--rmi", "all"], ["down"]):
            with self.subTest(argv=argv):
                recorder = Recorder()
                with mock.patch.object(start_prod, "_run", recorder):
                    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                        with self.assertRaises(SystemExit) as raised:
                            stop_prod.main(argv)
                self.assertEqual(raised.exception.code, 2, argv)
                self.assertEqual(recorder.commands, [])

    def test_import_has_no_side_effects_and_uses_absolute_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary)
            (target / "home").mkdir()
            environment = dict(os.environ, HOME=str(target / "home"), PYTHONPATH=str(ROOT))
            result = subprocess.run(
                [sys.executable, "-c", "import stop_prod; print(stop_prod.start_prod.MAIN_COMPOSE)"],
                cwd=target,
                env=environment,
                capture_output=True,
                text=True,
                timeout=60,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(Path(result.stdout.strip()), ROOT / "docker-compose.yml")
            self.assertTrue(Path(result.stdout.strip()).is_absolute())
            self.assertFalse((target / ".env").exists())
            self.assertFalse((target / ".tools").exists())
            self.assertFalse((target / "home" / "Library").exists())


if __name__ == "__main__":
    unittest.main()
