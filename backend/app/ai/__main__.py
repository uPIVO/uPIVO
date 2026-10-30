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
from app.ai.dto import SummaryResult
from app.ai.factory import create_summary_adapter
from app.ai.service import SummarizationService
from app.core.config import get_settings


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        self.print_usage(sys.stderr)
        self.exit(2, "Ошибка: некорректные аргументы командной строки.\n")


def main(argv: list[str] | None = None) -> int:
    parser = _Parser(description="Суммаризация единицы контента через настроенный AI-провайдер.")
    parser.add_argument("input", type=Path, help="JSON-файл с ContentUnitInput")
    parser.add_argument(
        "--prior-result",
        type=Path,
        help="Файл с ранее сохранённым результатом для повторного использования",
    )
    parser.add_argument(
        "--save-result",
        type=Path,
        help="Сохранить результат и fingerprint для последующего запуска",
    )
    parser.add_argument(
        "--model",
        help="Переопределить модель только для этого запуска",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Принудительно пересчитать выжимку, даже если найден актуальный результат",
    )
    args = parser.parse_args(argv)

    try:
        unit_payload = _read_json(args.input, "входной файл")
        prior_result = _read_result(args.prior_result) if args.prior_result is not None else None
        adapter = create_summary_adapter(get_settings(), model=args.model)
        run = asyncio.run(
            SummarizationService(adapter).summarize(
                unit_payload,
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


def _read_result(path: Path) -> SummaryResult:
    payload = _read_json(path, "файл предыдущего результата")
    try:
        return SummaryResult.model_validate(payload)
    except ValidationError:
        raise LLMError("Файл предыдущего результата имеет некорректную структуру.") from None


def _save_result(path: Path, result: SummaryResult) -> None:
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
        raise LLMError("Не удалось сохранить файл результата.") from None


if __name__ == "__main__":
    raise SystemExit(main())
