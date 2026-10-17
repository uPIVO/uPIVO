from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from app.ai.base import LLMError
from app.ai.dto import RelevanceResult, SummaryResult
from app.ai.jev import JevRelevanceAdapter
from app.ai.service import RelevanceService
from app.core.config import get_settings


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        self.print_usage(sys.stderr)
        self.exit(2, "Ошибка: некорректные аргументы командной строки.\n")


def main(argv: list[str] | None = None) -> int:
    parser = _Parser(
        description="Оценка релевантности готовой выжимки через Jev / TypeSafe."
    )
    parser.add_argument(
        "summary_result",
        type=Path,
        help="JSON с ранее сохранённым результатом суммаризации",
    )
    parser.add_argument(
        "--interests",
        required=True,
        type=Path,
        help="JSON-файл с полным списком интересов пользователя",
    )
    parser.add_argument(
        "--prior-result",
        type=Path,
        help="JSON с ранее сохранённой оценкой для повторного использования",
    )
    parser.add_argument(
        "--save-result",
        type=Path,
        help="Сохранить оценку и fingerprint для последующего запуска",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Принудительно получить новую оценку, даже если есть актуальный результат",
    )
    args = parser.parse_args(argv)

    try:
        summary_result = _read_summary_result(args.summary_result)
        interests = _read_json(args.interests, "файл со списком интересов")
        prior_result = (
            _read_relevance_result(args.prior_result)
            if args.prior_result is not None
            else None
        )
        if args.save_result is not None:
            protected_inputs = {args.summary_result.resolve(), args.interests.resolve()}
            if args.save_result.resolve() in protected_inputs:
                raise LLMError("Файл оценки не должен совпадать с входным файлом.")

        adapter = JevRelevanceAdapter(get_settings())
        run = asyncio.run(
            RelevanceService(adapter).score_relevance(
                {"summary": summary_result.summary, "interests": interests},
                prior_result=prior_result,
                force=args.force,
            )
        )
        if args.save_result is not None:
            _save_result(args.save_result, run.result)
    except LLMError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except ValidationError:
        print("Некорректная конфигурация или структура результата.", file=sys.stderr)
        return 2
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        print("Не удалось прочитать или сохранить JSON-файл.", file=sys.stderr)
        return 2

    output: dict[str, Any] = {
        "status": "переиспользован" if run.reused else "создан",
        "reused": run.reused,
        "elapsed_seconds": run.elapsed_seconds,
        "result": run.result.model_dump(mode="json", exclude_none=True),
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


def _read_json(path: Path, label: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise LLMError(f"Не найден {label}.") from None
    except json.JSONDecodeError:
        raise LLMError(f"Некорректный JSON: {label}.") from None
    except UnicodeDecodeError:
        raise LLMError(f"Файл с неверной кодировкой UTF-8: {label}.") from None


def _read_summary_result(path: Path) -> SummaryResult:
    payload = _read_json(path, "результат суммаризации")
    try:
        return SummaryResult.model_validate(payload)
    except ValidationError:
        raise LLMError("Файл результата суммаризации имеет некорректную структуру.") from None


def _read_relevance_result(path: Path) -> RelevanceResult:
    payload = _read_json(path, "файл предыдущей оценки релевантности")
    try:
        return RelevanceResult.model_validate(payload)
    except ValidationError:
        raise LLMError("Файл предыдущей оценки имеет некорректную структуру.") from None


def _save_result(path: Path, result: RelevanceResult) -> None:
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            temporary_file.write(result.model_dump_json(indent=2, exclude_none=True))
            temporary_file.write("\n")
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_path, path)
    except OSError:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
        raise LLMError("Не удалось сохранить файл оценки релевантности.") from None


if __name__ == "__main__":
    raise SystemExit(main())
