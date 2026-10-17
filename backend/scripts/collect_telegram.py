"""Сохранить новые публикации Telegram в PostgreSQL."""

import argparse
import asyncio
import sys
from collections.abc import Sequence

from sqlalchemy.orm import Session, sessionmaker

from app.connectors.telegram.client import TelegramConnector
from app.connectors.telegram.errors import TelegramConnectorError
from app.db.session import get_engine
from app.services.telegram import TelegramSyncError, sync_telegram_channel


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Сохранить публикации канала Telegram в PostgreSQL.", add_help=False
    )
    positional = parser.add_argument_group("Аргументы")
    positional.add_argument("source", help="Имя канала, ссылка t.me или полный ID канала.")
    options = parser.add_argument_group("Параметры")
    options.add_argument("-h", "--help", action="help", help="Показать справку и завершить работу.")
    options.add_argument(
        "--limit", type=int, help="Ограничить количество логических публикаций за запуск."
    )
    return parser


async def collect(source: str, *, limit: int | None = None) -> tuple[str, int, int, int]:
    session_factory = sessionmaker(bind=get_engine(), class_=Session, expire_on_commit=False)
    async with TelegramConnector() as connector:
        channel = await connector.resolve_channel(source)
        count, cursor = await sync_telegram_channel(
            channel, connector, session_factory, limit=limit
        )
    return channel.title, count, cursor, channel.channel_id


async def _run(args: argparse.Namespace) -> int:
    title, count, cursor, _channel_id = await collect(args.source, limit=args.limit)
    safe_title = " ".join(title.split()) or "канал без названия"
    print(f"Канал: {safe_title}")
    print(f"Сохранено логических публикаций: {count}")
    print(f"Последний успешно сохранённый message_id: {cursor}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.limit is not None and args.limit <= 0:
        parser.error("--limit должен быть положительным числом")
    try:
        return asyncio.run(_run(args))
    except (TelegramConnectorError, TelegramSyncError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except Exception:
        print(
            "Не удалось сохранить публикации Telegram; проверьте подключение к базе данных "
            "и повторите запуск.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
