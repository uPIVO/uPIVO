"""Просмотр сохранённых публикаций и релевантности пользователя демостенда.

Только чтение: один запрос в PostgreSQL проекта upivo-demo через его же контейнер.
Инструкция: docs/LOCAL_DEMO.md.
"""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MAIN_PROJECT = "upivo-demo"
MAIN_COMPOSE = ROOT / "docker-compose.yml"

DEFAULT_LIMIT = 50
MAX_LIMIT = 1000
DEFAULT_WIDTH = 100
MAX_WIDTH = 500
EXEC_TIMEOUT = 30

EMPTY = "—"
NO_TITLE = "Без названия"
NOTE = "Сохранённые оценки [0,1]; актуальность относительно текущих интересов и версии модели не проверяется."

# Фиксированный shell-скрипт: значения передаются позиционными аргументами и попадают
# в psql только как переменные, без подстановки текста в SQL.
PSQL_SCRIPT = (
    'exec psql -X -qAt -U "$POSTGRES_USER" -d "$POSTGRES_DB" '
    "--set=ON_ERROR_STOP=1 --set=identifier=\"$1\" --set=row_limit=\"$2\""
)

SQL_QUERY = """
BEGIN READ ONLY;
WITH target AS (
    SELECT u.id, u.email
    FROM users AS u
    WHERE (:'identifier' ~ '^[0-9]+$' AND u.id::text = :'identifier')
       OR u.email = :'identifier'
),
items AS (
    SELECT
        fi.id,
        s.name AS source_name,
        fi.title,
        fi.summary,
        fi.body_text,
        fi.original_url,
        fi.published_at,
        fi.collected_at,
        fi.processing_status,
        ci.cluster_id
    FROM feed_items AS fi
    JOIN sources AS s ON s.id = fi.source_id
    LEFT JOIN cluster_items AS ci ON ci.feed_item_id = fi.id
    WHERE EXISTS (
        SELECT 1
        FROM user_sources AS us
        WHERE us.user_id = (SELECT id FROM target)
          AND us.source_id = fi.source_id
          AND us.enabled
    )
    ORDER BY fi.published_at DESC NULLS LAST, fi.collected_at DESC, fi.id DESC
    LIMIT :'row_limit'::integer
)
SELECT json_build_object(
    'user', (SELECT json_build_object('id', id, 'email', email) FROM target),
    'items', COALESCE((
        SELECT json_agg(json_build_object(
            'id', i.id,
            'source', i.source_name,
            'title', left(COALESCE(i.title, ''), 1000),
            'summary', left(COALESCE(i.summary, ''), 1000),
            'body', left(i.body_text, 400),
            'url', i.original_url,
            'status', i.processing_status,
            'published_at', i.published_at,
            'collected_at', i.collected_at,
            'unit_kind', CASE WHEN i.cluster_id IS NULL THEN 'feed_item' ELSE 'cluster' END,
            'unit_id', COALESCE(i.cluster_id, i.id),
            'score', CASE WHEN i.cluster_id IS NULL THEN (
                    SELECT uis.relevance_score FROM user_item_scores AS uis
                    WHERE uis.user_id = (SELECT id FROM target) AND uis.feed_item_id = i.id
                ) ELSE (
                    SELECT uis.relevance_score FROM user_item_scores AS uis
                    WHERE uis.user_id = (SELECT id FROM target) AND uis.cluster_id = i.cluster_id
                ) END,
            'calculated_at', CASE WHEN i.cluster_id IS NULL THEN (
                    SELECT uis.calculated_at FROM user_item_scores AS uis
                    WHERE uis.user_id = (SELECT id FROM target) AND uis.feed_item_id = i.id
                ) ELSE (
                    SELECT uis.calculated_at FROM user_item_scores AS uis
                    WHERE uis.user_id = (SELECT id FROM target) AND uis.cluster_id = i.cluster_id
                ) END
        ) ORDER BY i.published_at DESC NULLS LAST, i.collected_at DESC, i.id DESC)
        FROM items AS i
    ), '[]'::json)
)::text;
ROLLBACK;
"""

_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]+")
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_DATE = re.compile(r"^(\d{4}-\d{2}-\d{2})")


class InspectorError(RuntimeError):
    """Ошибка, после которой просмотр прекращается с ненулевым кодом."""


def _env_file():
    return ROOT / ".env"


def _clean(value):
    if value is None:
        return ""
    text = _ANSI.sub("", str(value))
    return " ".join(_CONTROL.sub(" ", text).split())


def _format_score(score):
    if score is None:
        return EMPTY
    try:
        return f"{float(score):.3f}"
    except (TypeError, ValueError):
        return EMPTY


def _format_date(item):
    for key in ("published_at", "collected_at"):
        match = _DATE.match(_clean(item.get(key)))
        if match:
            return match.group(1)
    return EMPTY


def _truncate(text, width):
    return text if len(text) <= width else text[: width - 1] + "…"


def format_item(item, width):
    """Одна публикация — одна строка: без переводов строк и управляющих символов."""
    kind = "cluster" if item.get("unit_kind") == "cluster" else "feed_item"
    unit_id = item.get("unit_id") if item.get("unit_id") is not None else item.get("id")
    title = _clean(item.get("title")) or _clean(item.get("summary")) or _clean(item.get("body")) or NO_TITLE
    columns = (
        _clean(item.get("id")) or EMPTY,
        f"rel={_format_score(item.get('score'))}",
        f"unit={kind}:{_clean(unit_id) or EMPTY}",
        f"proc={_clean(item.get('status')) or EMPTY}",
        _format_date(item),
        _clean(item.get("source")) or EMPTY,
        _truncate(title, width),
        _clean(item.get("url")) or EMPTY,
    )
    return " | ".join(columns)


def _require_env_file():
    env_file = _env_file()
    if not env_file.is_file() or env_file.stat().st_size == 0:
        raise InspectorError(
            "Нет заполненного .env рядом с docker-compose.yml: создайте его по docs/LOCAL_DEMO.md."
        )
    return env_file


def _execute(command, sql):
    return subprocess.run(
        command,
        input=sql,
        capture_output=True,
        text=True,
        timeout=EXEC_TIMEOUT,
        check=False,
    )


def _run_query(identifier, limit):
    env_file = _require_env_file()
    command = [
        "docker",
        "compose",
        "-p",
        MAIN_PROJECT,
        "-f",
        str(MAIN_COMPOSE),
        "--env-file",
        str(env_file),
        "exec",
        "-T",
        "postgres",
        "sh",
        "-c",
        PSQL_SCRIPT,
        "inspect_user_feed",
        identifier,
        str(limit),
    ]
    try:
        result = _execute(command, SQL_QUERY)
    except subprocess.TimeoutExpired as exc:
        raise InspectorError(
            f"Docker Compose не ответил за {EXEC_TIMEOUT} с: проверьте Docker и повторите запрос."
        ) from exc
    except OSError as exc:
        raise InspectorError("Не удалось запустить docker: проверьте установку Docker и Docker Compose.") from exc
    if result.returncode != 0:
        raise InspectorError(
            f"Запрос к PostgreSQL проекта {MAIN_PROJECT} не удался: проверьте, что стенд запущен."
        )
    return _parse_payload(result.stdout)


def _parse_payload(text):
    # psql может вывести служебные строки вокруг единственного результата:
    # берём последнюю разобранную JSON-строку, а не весь вывод целиком.
    for line in reversed([line.strip() for line in (text or "").splitlines() if line.strip()]):
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        if isinstance(payload, dict):
            return payload
    raise InspectorError("PostgreSQL вернул неожиданный ответ: повторите запрос.")


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Просмотр сохранённых публикаций и релевантности пользователя демостенда."
    )
    parser.add_argument("--user", required=True, help="ID или email пользователя демостенда")
    parser.add_argument(
        "--limit", type=int, default=DEFAULT_LIMIT, help=f"сколько последних публикаций показать (1–{MAX_LIMIT})"
    )
    parser.add_argument(
        "--width", type=int, default=DEFAULT_WIDTH, help=f"длина краткого текста в символах (1–{MAX_WIDTH})"
    )
    arguments = parser.parse_args(argv)

    identifier = (arguments.user or "").strip()
    if not identifier:
        parser.error("--user не может быть пустым: укажите ID или email.")
    if not 1 <= arguments.limit <= MAX_LIMIT:
        parser.error(f"--limit должен быть от 1 до {MAX_LIMIT}.")
    if not 1 <= arguments.width <= MAX_WIDTH:
        parser.error(f"--width должен быть от 1 до {MAX_WIDTH}.")

    try:
        payload = _run_query(identifier, arguments.limit)
    except InspectorError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr, flush=True)
        return 1

    user = payload.get("user")
    if not isinstance(user, dict):
        print(
            f"Ошибка: пользователь {_truncate(_clean(identifier), 120)} не найден в проекте {MAIN_PROJECT}.",
            file=sys.stderr,
            flush=True,
        )
        return 2

    items = payload.get("items")
    if not isinstance(items, list):
        print("Ошибка: PostgreSQL вернул неожиданный ответ: повторите запрос.", file=sys.stderr, flush=True)
        return 1

    print(
        f"Пользователь: id={_clean(user.get('id'))} email={_clean(user.get('email')) or EMPTY}",
        file=sys.stderr,
        flush=True,
    )
    print(NOTE, file=sys.stderr, flush=True)
    if not items:
        print("Публикаций нет: у пользователя нет включённых источников с публикациями.", file=sys.stderr, flush=True)
        return 0
    for item in items:
        if isinstance(item, dict):
            print(format_item(item, arguments.width), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
