"""Интерактивная проверка сущностей FeedItem
без изменения данных в БД."""

from __future__ import annotations

import argparse
import errno
import json
import socket
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.db.session import get_engine
from app.models.enums import ProcessingStatus, SourceType
from app.models.feed_item import FeedItem
from app.models.source import Source
from app.processing.extractors.entity_extractor import extract_entities


def main(argv: list[str] | None = None, *, input_fn: Callable[[str], str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Интерактивная проверка нормализованных FeedItem "
            "без изменения данных в БД."
        )
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--fixture",
        type=Path,
        help="JSON-файл для автономной проверки без БД",
    )
    mode.add_argument(
        "--item-id",
        type=_positive_item_id,
        help=(
            "Проверить нормализованную публикацию по ID "
            "без интерактивного выбора"
        ),
    )
    args = parser.parse_args(argv)

    if args.fixture is not None:
        try:
            source_type, item = _load_fixture(args.fixture)
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            print(f"Не удалось прочитать fixture: {exc}", file=sys.stderr)
            return 2
        _display_item_and_result(item, source_type)
        return 0

    return _run_database(item_id=args.item_id, input_fn=input_fn or input)


MAX_RECENT_ITEMS = 20


def _positive_item_id(value: str) -> int:
    try:
        item_id = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "ID должен быть целым числом"
        ) from exc
    if item_id <= 0:
        raise argparse.ArgumentTypeError(
            "ID должен быть положительным числом"
        )
    return item_id


def _open_session() -> Session:
    return Session(get_engine())


def _list_recent_normalized_items(
    session: Session,
) -> list[tuple[FeedItem, SourceType]]:
    statement = (
        select(FeedItem, Source.type)
        .join(Source, Source.id == FeedItem.source_id)
        .where(FeedItem.processing_status == ProcessingStatus.NORMALIZED)
        .order_by(FeedItem.published_at.desc().nulls_last(), FeedItem.id.desc())
        .limit(MAX_RECENT_ITEMS)
    )
    with session.no_autoflush:
        return [
            (item, source_type)
            for item, source_type in session.execute(statement).all()
        ]


def _find_normalized_item(
    session: Session, item_id: int
) -> tuple[FeedItem, SourceType] | None:
    statement = (
        select(FeedItem, Source.type)
        .join(Source, Source.id == FeedItem.source_id)
        .where(
            FeedItem.id == item_id,
            FeedItem.processing_status == ProcessingStatus.NORMALIZED,
        )
    )
    with session.no_autoflush:
        row = session.execute(statement).first()
    if row is None:
        return None
    return row[0], row[1]


def _database_error_hint(error: SQLAlchemyError) -> str:
    pending: list[BaseException] = [error]
    seen: set[int] = set()
    sqlstates: set[str] = set()
    error_numbers: set[int] = set()
    hostname_resolution_failed = False

    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))

        diagnostic = getattr(current, "diag", None)
        for candidate in (
            getattr(current, "sqlstate", None),
            getattr(current, "pgcode", None),
            getattr(diagnostic, "sqlstate", None),
        ):
            if (
                isinstance(candidate, str)
                and len(candidate) == 5
                and candidate.isascii()
                and candidate.isalnum()
            ):
                sqlstates.add(candidate.upper())

        if isinstance(current, socket.gaierror):
            hostname_resolution_failed = True
        error_number = getattr(current, "errno", None)
        if isinstance(error_number, int):
            error_numbers.add(error_number)

        for attribute in ("orig", "__cause__", "__context__"):
            cause = getattr(current, attribute, None)
            if isinstance(cause, BaseException):
                pending.append(cause)

    if any(code.startswith("28") for code in sqlstates):
        return (
            "PostgreSQL отклонил аутентификацию; проверьте учётные данные "
            "в локальных настройках подключения."
        )
    if "3D000" in sqlstates:
        return "База данных не найдена; проверьте её имя в локальных настройках."
    if "42501" in sqlstates:
        return "У роли нет прав на чтение; проверьте права пользователя БД."
    if "57P03" in sqlstates:
        return "PostgreSQL временно не принимает соединения; проверьте состояние сервера."
    if any(code.startswith("08") for code in sqlstates):
        return (
            "Не удалось установить соединение; проверьте адрес, порт "
            "и доступность PostgreSQL."
        )
    if hostname_resolution_failed:
        return (
            "Не удалось разрешить имя сервера БД; проверьте hostname "
            "в локальных настройках подключения."
        )
    if errno.ECONNREFUSED in error_numbers:
        return (
            "Соединение отклонено; проверьте, что PostgreSQL запущен "
            "на настроенных адресе и порту."
        )
    if errno.ETIMEDOUT in error_numbers:
        return (
            "Истекло ожидание соединения; проверьте доступность PostgreSQL "
            "и сетевой маршрут."
        )
    network_errors = {
        getattr(errno, "EHOSTUNREACH", None),
        getattr(errno, "ENETUNREACH", None),
        getattr(errno, "ECONNRESET", None),
    } - {None}
    if network_errors & error_numbers:
        return "Сеть недоступна или соединение сброшено; проверьте маршрут до PostgreSQL."
    return (
        "Проверьте доступность базы данных PostgreSQL и локальные настройки "
        "подключения. Подробности драйвера не выводятся, чтобы не раскрыть секреты."
    )


def _run_database(
    *, item_id: int | None, input_fn: Callable[[str], str]
) -> int:
    try:
        with _open_session() as session:
            if item_id is None:
                rows = _list_recent_normalized_items(session)
                if not rows:
                    print(
                        "Нет нормализованных публикаций "
                        "для проверки."
                    )
                    return 0
                _print_recent_items(rows)
                try:
                    selection_prompt = "ID публикации для проверки: "
                    selection = input_fn(selection_prompt).strip()
                    selected_id = int(selection)
                except ValueError:
                    print("Введите числовой ID из списка.", file=sys.stderr)
                    return 2
                row = next(
                    (
                        candidate
                        for candidate in rows
                        if candidate[0].id == selected_id
                    ),
                    None,
                )
                if row is None:
                    print(
                        "Публикация с этим ID отсутствует "
                        "в списке.",
                        file=sys.stderr,
                    )
                    return 2
            else:
                row = _find_normalized_item(session, item_id)
                if row is None:
                    print(
                        "Публикация не найдена "
                        "или ещё не нормализована.",
                        file=sys.stderr,
                    )
                    return 2
            item, source_type = row
            _display_item_and_result(item, source_type)
        return 0
    except EOFError:
        print("Ввод ID прерван.", file=sys.stderr)
        return 2
    except SQLAlchemyError as exc:
        hint = _database_error_hint(exc)
        error_type = type(exc).__name__
        print(
            f"Ошибка чтения публикаций из БД ({error_type}). {hint}",
            file=sys.stderr,
        )
        return 1


def _print_recent_items(rows: list[tuple[FeedItem, SourceType]]) -> None:
    print(
        "Нормализованные публикации "
        f"(последние {MAX_RECENT_ITEMS}):"
    )
    for item, source_type in rows:
        title = " ".join((item.title or "Без заголовка").split())
        if len(title) > 120:
            title = f"{title[:117]}..."
        print(f"{item.id}\t{source_type.value}\t{title}")


def _display_item_and_result(item: FeedItem, source_type: SourceType) -> None:
    extraction_input = {
        "item_id": item.id,
        "source_type": source_type.value,
        "title": item.title,
        "body_text": item.body_text,
        "raw_payload": item.raw_payload,
        "original_url": item.original_url,
    }
    print("=== Входные данные извлечения ===")
    print(json.dumps(extraction_input, ensure_ascii=False, indent=2))
    print("=== Результат извлечения (не сохранён) ===")
    result = extract_entities(item, source_type)
    print(json.dumps(result, ensure_ascii=False, indent=2))


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
    # Объект предназначен только для локальной проверки
    # fixture; CLI не сохраняет его в БД.
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
