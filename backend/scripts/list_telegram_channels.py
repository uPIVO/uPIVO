import argparse
import asyncio
import json
import sys
from collections.abc import Sequence

from app.connectors.telegram.client import TelegramConnector
from app.connectors.telegram.errors import TelegramConnectorError
from app.connectors.telegram.models import TelegramChannelInfo


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Показать названия и полные ID каналов Telegram-аккаунта.",
        add_help=False,
    )
    help_group = parser.add_argument_group("Параметры")
    help_group.add_argument(
        "-h",
        "--help",
        action="help",
        help="Показать эту справку и завершить работу.",
    )
    return parser


async def _list_channels() -> list[TelegramChannelInfo]:
    async with TelegramConnector() as connector:
        return await connector.list_broadcast_channels()


def main(argv: Sequence[str] | None = None) -> int:
    build_parser().parse_args(argv)
    try:
        channels = asyncio.run(_list_channels())
    except TelegramConnectorError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except Exception:
        print("Не удалось получить список каналов Telegram.", file=sys.stderr)
        return 1

    if not channels:
        print("Каналы не найдены.")
        return 0

    print(f"Каналов: {len(channels)}")
    print("ID канала\tНазвание")
    for channel in channels:
        print(f"{channel.marked_id}\t{json.dumps(channel.title, ensure_ascii=False)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
