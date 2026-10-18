"""Локальный интерактивный запуск извлекателя
без подключения к БД."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from app.models.enums import SourceType
from app.models.feed_item import FeedItem
from app.processing.extractors.entity_extractor import extract_entities


def main(argv: list[str] | None = None, *, input_fn: Callable[[str], str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Проверить извлечение сущностей из FeedItem "
            "без БД и сетевых запросов."
        )
    )
    parser.add_argument(
        "--fixture",
        type=Path,
        help=(
            "JSON-файл с source_type, title, body_text "
            "и необязательным raw_payload"
        ),
    )
    args = parser.parse_args(argv)

    try:
        if args.fixture is not None:
            source_type, item = _load_fixture(args.fixture)
        else:
            source_type, item = _read_interactively(input_fn or input)
    except (OSError, json.JSONDecodeError, ValueError, EOFError) as exc:
        print(f"Не удалось подготовить FeedItem: {exc}", file=sys.stderr)
        return 2

    print(json.dumps(extract_entities(item, source_type), ensure_ascii=False, indent=2))
    return 0


def _load_fixture(path: Path) -> tuple[SourceType, FeedItem]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, Mapping):
        raise ValueError("fixture должен содержать JSON-объект")
    source_type = _source_type(data.get("source_type"))
    title = data.get("title")
    body_text = data.get("body_text", "")
    original_url = data.get("original_url", "")
    for name, value in (("title", title), ("body_text", body_text), ("original_url", original_url)):
        if value is not None and not isinstance(value, str):
            raise ValueError(f"поле {name} должно быть строкой")
    return source_type, _make_item(title, body_text, original_url or "", data.get("raw_payload"))


def _read_interactively(input_fn: Callable[[str], str]) -> tuple[SourceType, FeedItem]:
    accepted = ", ".join(source.value for source in SourceType)
    source_type = _source_type(input_fn(f"Тип источника ({accepted}): "))
    title = input_fn("Заголовок (можно оставить пустым): ") or None
    body_text = input_fn("Текст публикации (одна строка): ")
    raw_text = input_fn("raw_payload в JSON (Enter — без метаданных): ").strip()
    raw_payload = json.loads(raw_text) if raw_text else None
    return source_type, _make_item(title, body_text, "", raw_payload)


def _source_type(value: object) -> SourceType:
    if not isinstance(value, str):
        raise ValueError("source_type должен быть строкой")
    try:
        return SourceType(value.strip().lower())
    except ValueError as exc:
        options = ", ".join(source.value for source in SourceType)
        raise ValueError(
            f"неизвестный source_type; допустимые значения: {options}"
        ) from exc


def _make_item(
    title: str | None,
    body_text: str | None,
    original_url: str,
    raw_payload: Any,
) -> FeedItem:
    # Транзиентный ORM-объект: CLI не открывает сессию
    # и не сохраняет изменения.
    return FeedItem(
        source_id=0,
        external_id="interactive-test",
        title=title,
        body_text=body_text or "",
        original_url=original_url,
        raw_payload=raw_payload,
    )


if __name__ == "__main__":
    raise SystemExit(main())
