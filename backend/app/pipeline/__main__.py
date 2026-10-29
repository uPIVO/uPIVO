"""CLI: сбор, нормализация, склейка и общий AI-этап для одного пользователя.

Запуск из ``backend/`` при поднятой PostgreSQL и применённых миграциях:

    uv run python -m app.pipeline --user demo@example.com
    uv run python -m app.pipeline --user 1 --dry-run
    uv run python -m app.pipeline --user 1 --ai-limit 10
    uv run python -m app.pipeline --fixture tests/fixtures/pipeline/sample.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any

from app.clustering.cluster import format_report
from app.connectors.youtube import DEFAULT_LIMIT
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.pipeline.analysis import DEFAULT_AI_LIMIT, AnalysisStats
from app.pipeline.run import FixturePipelineResult, PipelineResult, chain_payloads, run_pipeline
from app.services.collect import UnknownUserError
from app.services.source_sync import SourceSyncResult


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Сбор, нормализация, склейка и общий AI-этап для одного пользователя. "
            "Источники должны быть уже включены. AI-этап вызывает платные "
            "провайдеры и ставит задачи персональной оценки в брокер."
        )
    )
    parser.add_argument(
        "--user",
        help="users.id или users.email с включёнными источниками",
    )
    parser.add_argument(
        "--fixture",
        type=Path,
        help="JSON-объект или список объектов: нормализация и склейка без базы",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help=(
            "Максимум публикаций на источник и pending-строк "
            f"(по умолчанию для YouTube {DEFAULT_LIMIT})"
        ),
    )
    parser.add_argument(
        "--ai-limit",
        type=int,
        default=DEFAULT_AI_LIMIT,
        help=(
            "Максимум единиц общего AI-этапа за запуск "
            f"(по умолчанию {DEFAULT_AI_LIMIT}); не связан с --limit"
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Не опрашивать источники, не писать этапы и не вызывать AI",
    )
    args = parser.parse_args(argv)
    configure_logging(get_settings().log_level)

    if args.limit is not None and args.limit < 1:
        print("limit must be >= 1", file=sys.stderr)
        return 2
    if args.ai_limit < 1:
        print("ai-limit: ожидается положительное число", file=sys.stderr)
        return 2
    if args.user is None and args.fixture is None:
        print("Pass --user or --fixture", file=sys.stderr)
        return 2
    if args.user is not None and args.fixture is not None:
        print("--user and --fixture cannot be used together", file=sys.stderr)
        return 2

    if args.fixture is not None:
        return _run_fixture(args.fixture, limit=args.limit, dry_run=args.dry_run)
    return _run_user(args.user, limit=args.limit, ai_limit=args.ai_limit, dry_run=args.dry_run)


def _run_fixture(path: Path, *, limit: int | None, dry_run: bool) -> int:
    try:
        payloads = _load_fixture(path)
    except FileNotFoundError:
        print(f"Fixture not found: {path}", file=sys.stderr)
        return 2
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"Invalid fixture: {exc}", file=sys.stderr)
        return 2
    if limit is not None:
        payloads = payloads[:limit]
    try:
        result = chain_payloads(payloads)
    except ValueError as exc:
        print(f"Invalid fixture: {exc}", file=sys.stderr)
        return 2
    _print_fixture(result, dry_run=dry_run)
    return 0


def _run_user(identifier: str, *, limit: int | None, ai_limit: int, dry_run: bool) -> int:
    logger = logging.getLogger("app.pipeline")
    try:
        result = asyncio.run(
            run_pipeline(identifier, limit=limit, ai_limit=ai_limit, dry_run=dry_run)
        )
    except UnknownUserError as exc:
        print(f"Unknown user: {exc}", file=sys.stderr)
        return 2
    except Exception:
        logger.exception("Failed to run the publication pipeline")
        print("Failed to run the publication pipeline", file=sys.stderr)
        return 1
    _print_result(result)
    if result.collect.failed:
        return 1
    return 1 if result.analysis is not None and result.analysis.has_failures else 0


def _print_result(result: PipelineResult) -> None:
    print(f"user_id={result.user_id} email={result.email}")
    if not result.collect.outcomes:
        print("sources=0")
    for row in result.collect.outcomes:
        print(_format_source(row))
    collected = result.collect
    print(
        f"collected={collected.collected} empty={collected.empty} "
        f"skipped={collected.skipped} failed={collected.failed}"
    )
    stats = result.normalize
    suffix = " dry-run" if result.dry_run else ""
    print(f"processed={stats.processed} failed={stats.failed} skipped={stats.skipped}{suffix}")
    print(
        format_report(
            result.clusters,
            unclustered=result.unclustered,
            dry_run=result.dry_run,
        )
    )
    if result.analysis is not None:
        print(_format_analysis(result.analysis))


def _format_analysis(stats: AnalysisStats) -> str:
    return (
        f"ai_selected={stats.selected} ai_succeeded={stats.succeeded} "
        f"ai_reused={stats.reused} ai_failed={stats.failed} ai_skipped={stats.skipped} "
        f"ai_summary_failed={stats.summary_failed} "
        f"ai_importance_failed={stats.importance_failed} "
        f"queue_succeeded={stats.queue_succeeded} queue_failed={stats.queue_failed} "
        f"receipt_failed={stats.receipt_failed}"
    )


def _print_fixture(result: FixturePipelineResult, *, dry_run: bool) -> None:
    suffix = " dry-run" if dry_run else ""
    print(f"processed={result.processed} failed={result.failed}{suffix}")
    print(
        format_report(
            result.clusters,
            unclustered=result.unclustered,
            dry_run=dry_run,
        )
    )


def _format_source(row: SourceSyncResult) -> str:
    parts = [
        f"source_id={row.source_id}",
        f"type={row.source_type}",
        f"status={row.status}",
        f"fetched={row.fetched}",
        f"saved={row.saved}",
    ]
    if row.inserted is not None:
        parts.append(f"inserted={row.inserted}")
    if row.updated is not None:
        parts.append(f"updated={row.updated}")
    if row.reason:
        parts.append(f"reason={row.reason}")
    return " ".join(parts)


def _load_fixture(path: Path) -> list[Any]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict):
        return [raw]
    raise ValueError("fixture must be a JSON object or a list of objects")


if __name__ == "__main__":
    sys.exit(main())
