from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any, NoReturn

from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.ai.base import LLMError
from app.ai.importance import ImportanceService
from app.ai.jev import JevImportanceAdapter, JevRelevanceAdapter
from app.ai.openrouter import OpenRouterAdapter
from app.ai.service import RelevanceService, SummarizationService
from app.core.config import get_settings
from app.db.session import get_engine
from app.services.analyze_feed_item import (
    AnalysisOperation,
    AnalysisRequestError,
    AnalysisStepResult,
    AnalysisStorageError,
    ContentUnitAnalysisOutcome,
    ContentUnitAnalysisUseCase,
    FeedItemAnalysisUseCase,
)


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        self.exit(2, "Ошибка: некорректные аргументы командной строки.\n")


def _positive_int(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError:
        raise argparse.ArgumentTypeError("Ожидается положительное целое число.") from None
    if value <= 0:
        raise argparse.ArgumentTypeError("Ожидается положительное целое число.")
    return value


def _read_interests(path: Path) -> list[str]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        raise AnalysisRequestError("Не удалось прочитать JSON со списком интересов.") from None
    if not isinstance(value, list):
        raise AnalysisRequestError("Файл интересов должен содержать JSON-массив.")
    return value


def _step_json(step: AnalysisStepResult) -> dict[str, Any]:
    output: dict[str, Any] = {"status": step.status.value}
    if step.reused is not None:
        output["reused"] = step.reused
    if step.save_disposition is not None:
        output["save_disposition"] = step.save_disposition.value
    if step.result is not None:
        output["result"] = step.result.model_dump(mode="json", exclude_none=True)
    if step.error is not None:
        output["error"] = step.error
    return output


def _outcome_json(outcome: ContentUnitAnalysisOutcome) -> dict[str, Any]:
    operations: dict[str, dict[str, Any]] = {}
    for name in ("summary", "importance", "relevance"):
        step = getattr(outcome, name)
        if step is not None:
            operations[name] = _step_json(step)
    output = {
        "kind": outcome.kind,
        "unit_id": outcome.unit_id,
        "operation": outcome.operation.value,
        "status": outcome.status,
        "attempt_count": outcome.attempt_count,
        "operations": operations,
    }
    if outcome.kind == "feed_item":
        output["feed_item_id"] = outcome.unit_id
    return output


def main(argv: list[str] | None = None) -> int:
    parser = _Parser(description="Ручной анализ FeedItem или ContentCluster без Celery.")
    parser.add_argument("unit_id", type=_positive_int, help="ID единицы анализа")
    parser.add_argument(
        "--kind",
        choices=("feed_item", "cluster"),
        default="feed_item",
        help="Тип единицы: feed_item (по умолчанию) или cluster",
    )
    parser.add_argument(
        "--operation",
        choices=[operation.value for operation in AnalysisOperation],
        default=AnalysisOperation.ALL.value,
        help="Операция: summary, importance, relevance или all (по умолчанию)",
    )
    parser.add_argument("--user-id", type=_positive_int, help="ID существующего пользователя")
    parser.add_argument("--interests", type=Path, help="JSON-массив интересов для релевантности")
    parser.add_argument(
        "--summary-model",
        help="Модель OpenRouter только для summary или all",
    )
    parser.add_argument(
        "--importance-model",
        help="Модель TypeSafe только для importance или all",
    )
    parser.add_argument(
        "--relevance-model",
        help="Модель TypeSafe только для relevance или all",
    )
    parser.add_argument(
        "--force", action="store_true", help="Не переиспользовать актуальные результаты"
    )
    args = parser.parse_args(argv)

    operation = AnalysisOperation(args.operation)
    interests = None
    if operation.requires_user_context:
        if args.user_id is None:
            print("Для этой операции укажите --user-id.", file=sys.stderr)
            return 2
        if args.interests is None:
            print("Для этой операции укажите --interests.", file=sys.stderr)
            return 2
        try:
            interests = _read_interests(args.interests)
        except AnalysisRequestError as error:
            print(str(error), file=sys.stderr)
            return 2

    try:
        settings = get_settings()
        summarization_service = (
            SummarizationService(OpenRouterAdapter(settings, model=args.summary_model))
            if operation.includes_summary
            else None
        )
        importance_service = (
            ImportanceService(JevImportanceAdapter(settings, model=args.importance_model))
            if operation.includes_importance
            else None
        )
        relevance_service = (
            RelevanceService(JevRelevanceAdapter(settings, model=args.relevance_model))
            if operation.includes_relevance
            else None
        )
        engine = get_engine()
        use_case_options = {
            "session_factory": lambda: Session(engine),
            "summarization_service": summarization_service,
            "importance_service": importance_service,
            "relevance_service": relevance_service,
        }
        if args.kind == "feed_item":
            use_case = FeedItemAnalysisUseCase(**use_case_options)
        else:
            use_case = ContentUnitAnalysisUseCase(kind=args.kind, **use_case_options)
        outcome = asyncio.run(
            use_case.run(
                args.unit_id,
                operation=operation,
                user_id=args.user_id,
                interests=interests,
                force=args.force,
            )
        )
    except (AnalysisRequestError, AnalysisStorageError, LLMError) as error:
        print(str(error), file=sys.stderr)
        return 2
    except ValidationError:
        print("Некорректная конфигурация или входные данные анализа.", file=sys.stderr)
        return 2
    except Exception:
        print("Не удалось выполнить анализ единицы контента.", file=sys.stderr)
        return 2

    print(json.dumps(_outcome_json(outcome), ensure_ascii=False, indent=2))
    return 0 if outcome.succeeded else 1


if __name__ == "__main__":
    raise SystemExit(main())
