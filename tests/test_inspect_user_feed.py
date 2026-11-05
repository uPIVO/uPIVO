"""Контракты просмотра сохранённых публикаций и релевантности.

Тесты не запускают Docker и не ходят в PostgreSQL: выполнение команды подменяется.
"""

import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import inspect_user_feed  # noqa: E402

NOTE = "Сохранённые оценки [0,1]; актуальность относительно текущих интересов и версии модели не проверяется."
USER = {"id": 1, "email": "demo@example.com"}


def make_item(**overrides):
    item = {
        "id": 10,
        "source": "Example",
        "title": "Заголовок",
        "summary": "Кратко",
        "body": "Тело",
        "url": "https://example.com/a",
        "status": "processed",
        "published_at": "2026-02-03T04:05:06",
        "collected_at": "2026-02-03T05:06:07",
        "unit_kind": "feed_item",
        "unit_id": 10,
        "score": 0.88,
        "calculated_at": "2026-02-03T06:00:00",
    }
    item.update(overrides)
    return item


class FakeExecutor:
    """Заменяет выполнение docker compose exec: запоминает вызовы и отдаёт заданный ответ."""

    def __init__(self, *, code=0, out="", err="", error=None):
        self.code = code
        self.out = out
        self.err = err
        self.error = error
        self.calls = []

    def __call__(self, command, sql):
        self.calls.append(([str(part) for part in command], sql))
        if self.error is not None:
            raise self.error
        return subprocess.CompletedProcess([str(part) for part in command], self.code, self.out, self.err)


class InspectorTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.env_file = Path(temporary.name) / ".env"
        self.env_file.write_text("POSTGRES_PASSWORD=fixture\n", encoding="utf-8")

    def run_cli(self, argv, payload=None, *, out=None, code=0, err="", error=None):
        executor = FakeExecutor(
            code=code,
            out=out if out is not None else json.dumps(payload, ensure_ascii=False),
            err=err,
            error=error,
        )
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(inspect_user_feed, "_execute", executor):
            with mock.patch.object(inspect_user_feed, "_env_file", return_value=self.env_file):
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    result = inspect_user_feed.main(argv)
        return result, stdout.getvalue(), stderr.getvalue(), executor

    def test_publication_is_one_line_without_control_characters(self):
        item = make_item(
            title="Первая\nстрока\x1b[31m и вторая\x07",
            summary="Кратко\nв две строки",
            source="Источник\x00имя",
            url="https://example.com/a?x=\x1b[31m",
        )

        code, out, _err, _executor = self.run_cli(["--user", "1"], {"user": USER, "items": [item]})

        self.assertEqual(code, 0)
        self.assertEqual(len(out.splitlines()), 1, out)
        self.assertEqual(out.count("|"), 7, out)
        self.assertNotIn("\x1b", out)
        self.assertNotIn("\x00", out)
        self.assertNotIn("\x07", out)
        self.assertIn("Первая строка", out)
        self.assertIn("и вторая", out)
        self.assertIn("Источник имя", out)

    def test_score_is_shown_missing_zero_and_rounded(self):
        items = [make_item(score=None), make_item(id=11, score=0.0, unit_id=11), make_item(id=12, score=0.9876, unit_id=12)]

        code, out, _err, _executor = self.run_cli(["--user", "1"], {"user": USER, "items": items})

        self.assertEqual(code, 0)
        lines = out.splitlines()
        self.assertIn("rel=—", lines[0])
        self.assertIn("rel=0.000", lines[1])
        self.assertIn("rel=0.988", lines[2])

    def test_title_falls_back_to_summary_body_and_placeholder(self):
        items = [
            make_item(title="", summary="Краткое\nописание"),
            make_item(id=11, unit_id=11, title=None, summary="", body="Текст тела"),
            make_item(id=12, unit_id=12, title=None, summary=None, body="   "),
        ]

        code, out, _err, _executor = self.run_cli(["--user", "1"], {"user": USER, "items": items})

        self.assertEqual(code, 0)
        lines = out.splitlines()
        self.assertIn("Краткое описание", lines[0])
        self.assertIn("Текст тела", lines[1])
        self.assertIn("Без названия", lines[2])

    def test_title_is_truncated_to_width(self):
        long_title = "Слово " * 40

        code, out, _err, _executor = self.run_cli(
            ["--user", "1", "--width", "20"], {"user": USER, "items": [make_item(title=long_title)]}
        )

        self.assertEqual(code, 0)
        field = out.split("|")[6].strip()
        self.assertEqual(len(field), 20, field)
        self.assertTrue(field.endswith("…"), field)

    def test_cli_rejects_missing_empty_and_out_of_range_values(self):
        cases = (
            [],
            ["--user", ""],
            ["--user", "   "],
            ["--user", "1", "--limit", "0"],
            ["--user", "1", "--limit", "1001"],
            ["--user", "1", "--width", "0"],
            ["--user", "1", "--width", "501"],
        )
        for argv in cases:
            with self.subTest(argv=argv):
                stdout, stderr = io.StringIO(), io.StringIO()
                with mock.patch.object(inspect_user_feed, "_execute", FakeExecutor()):
                    with mock.patch.object(inspect_user_feed, "_env_file", return_value=self.env_file):
                        with redirect_stdout(stdout), redirect_stderr(stderr):
                            with self.assertRaises(SystemExit) as raised:
                                inspect_user_feed.main(argv)
                self.assertEqual(raised.exception.code, 2, argv)
                self.assertEqual(stdout.getvalue(), "", argv)

    def test_command_is_pinned_and_parameters_stay_out_of_sql(self):
        identifier = "o'brien\"\n-- ; DROP TABLE users; --"

        code, out, err, executor = self.run_cli(["--user", identifier, "--limit", "7"], {"user": USER, "items": []})

        self.assertEqual(code, 0, err)
        self.assertEqual(len(executor.calls), 1)
        command, sql = executor.calls[0]
        self.assertEqual(
            command,
            [
                "docker",
                "compose",
                "-p",
                "upivo-demo",
                "-f",
                str(inspect_user_feed.MAIN_COMPOSE),
                "--env-file",
                str(self.env_file),
                "exec",
                "-T",
                "postgres",
                "sh",
                "-c",
                inspect_user_feed.PSQL_SCRIPT,
                "inspect_user_feed",
                identifier,
                "7",
            ],
        )
        self.assertEqual(command.count(identifier), 1)
        self.assertNotIn(identifier, sql)
        self.assertNotIn("DROP TABLE", sql)
        self.assertIn('exec psql -X -qAt -U "$POSTGRES_USER" -d "$POSTGRES_DB"', inspect_user_feed.PSQL_SCRIPT)
        self.assertIn("--set=ON_ERROR_STOP=1", inspect_user_feed.PSQL_SCRIPT)
        self.assertIn('--set=identifier="$1"', inspect_user_feed.PSQL_SCRIPT)
        self.assertIn('--set=row_limit="$2"', inspect_user_feed.PSQL_SCRIPT)
        for marker in (
            "BEGIN READ ONLY",
            "ROLLBACK",
            ":'identifier'",
            ":'row_limit'::integer",
            "user_sources",
            "us.enabled",
            "cluster_items",
            "user_item_scores",
            "uis.cluster_id = i.cluster_id",
            "uis.feed_item_id = i.id",
            "fi.published_at DESC NULLS LAST",
            "fi.collected_at DESC",
            "fi.id DESC",
            "LIMIT :'row_limit'::integer",
        ):
            self.assertIn(marker, sql)
        self.assertLess(sql.index("BEGIN READ ONLY"), sql.index("ROLLBACK"))
        self.assertLessEqual(inspect_user_feed.EXEC_TIMEOUT, 60)

    def test_clustered_item_uses_cluster_unit_and_score(self):
        items = [make_item(), make_item(id=11, unit_kind="cluster", unit_id=7, score=0.5)]

        code, out, _err, _executor = self.run_cli(["--user", "1"], {"user": USER, "items": items})

        self.assertEqual(code, 0)
        lines = out.splitlines()
        self.assertEqual(lines[0], "10 | rel=0.880 | unit=feed_item:10 | proc=processed | 2026-02-03 | Example | Заголовок | https://example.com/a")
        self.assertIn("unit=cluster:7", lines[1])
        self.assertIn("rel=0.500", lines[1])
        self.assertIn("11 |", lines[1])

    def test_header_and_note_go_to_stderr_only(self):
        code, out, err, _executor = self.run_cli(["--user", "1"], {"user": USER, "items": [make_item()]})

        self.assertEqual(code, 0)
        self.assertIn(NOTE, err)
        self.assertIn("id=1", err)
        self.assertIn("demo@example.com", err)
        self.assertNotIn(NOTE, out)
        self.assertNotIn("id=1", out)

    def test_psql_status_lines_around_result_are_tolerated(self):
        payload = json.dumps({"user": USER, "items": [make_item()]}, ensure_ascii=False)

        code, out, err, _executor = self.run_cli(["--user", "1"], out=f"BEGIN\n{payload}\nROLLBACK\n")

        self.assertEqual(code, 0, err)
        self.assertEqual(len(out.splitlines()), 1, out)

    def test_unknown_user_is_reported_with_code_two(self):
        code, out, err, _executor = self.run_cli(["--user", "missing@example.com"], {"user": None, "items": []})

        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("не найден", err)

    def test_known_user_without_publications_succeeds(self):
        code, out, err, _executor = self.run_cli(["--user", "1"], {"user": USER, "items": []})

        self.assertEqual(code, 0)
        self.assertEqual(out, "")
        self.assertIn("Публикаций нет", err)

    def test_failures_are_concise_and_hide_details(self):
        secret = 'FATAL: password authentication failed for user "upivo_demo"'

        code, out, err, _executor = self.run_cli(["--user", "1"], {"user": USER}, code=1, err=secret)
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("Ошибка", err)
        self.assertNotIn("FATAL", err)
        self.assertNotIn("password", err)

        code, out, err, _executor = self.run_cli(["--user", "1"], out="not json at all")
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertNotIn("not json at all", err)

        code, out, err, _executor = self.run_cli(
            ["--user", "1"], error=subprocess.TimeoutExpired("docker", inspect_user_feed.EXEC_TIMEOUT)
        )
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn(str(inspect_user_feed.EXEC_TIMEOUT), err)

    def test_missing_env_file_stops_before_docker(self):
        self.env_file.unlink()

        code, out, err, executor = self.run_cli(["--user", "1"], {"user": USER, "items": []})

        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn(".env", err)
        self.assertEqual(executor.calls, [])

    def test_paths_are_absolute_and_independent_of_cwd(self):
        self.assertTrue(inspect_user_feed.MAIN_COMPOSE.is_absolute())
        self.assertEqual(inspect_user_feed.MAIN_COMPOSE, ROOT / "docker-compose.yml")
        self.assertTrue(inspect_user_feed._env_file().is_absolute())


if __name__ == "__main__":
    unittest.main()
