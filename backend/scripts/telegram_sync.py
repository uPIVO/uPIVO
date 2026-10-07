import argparse
import asyncio
import sys
from collections.abc import Sequence

from app.connectors.telegram.client import TelegramConnector
from app.connectors.telegram.errors import TelegramConnectorError
from app.connectors.telegram.models import TelegramPost


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Получить публикации канала Telegram в терминале.",
        add_help=False,
    )
    positional = parser.add_argument_group("Аргументы")
    positional.add_argument("source", help="Имя канала, ссылка t.me или полный ID канала.")
    options = parser.add_argument_group("Параметры")
    options.add_argument(
        "-h",
        "--help",
        action="help",
        help="Показать эту справку и завершить работу.",
    )
    options.add_argument(
        "--after-message-id",
        type=int,
        help="Получить публикации строго после этого ID сообщения.",
    )
    options.add_argument(
        "--limit",
        type=int,
        default=None,
        help=(
            "Количество логических публикаций: без параметра — последние 50 при начальной "
            "загрузке или все после указанного ID."
        ),
    )
    return parser


async def collect(
    source: str, *, after_message_id: int | None, limit: int | None
) -> tuple[str, int, list[TelegramPost]]:
    async with TelegramConnector() as connector:
        channel = await connector.resolve_channel(source)
        if after_message_id is None:
            initial_limit = 50 if limit is None else limit
            posts = await connector.fetch_latest_posts(channel, limit=initial_limit)
        else:
            posts = await connector.fetch_new_posts(
                channel, after_message_id=after_message_id, limit=limit
            )
    return channel.title, channel.channel_id, posts


def _print_posts(title: str, channel_id: int, posts: list[TelegramPost]) -> None:
    safe_title = " ".join(title.split()) or "канал без названия"
    print(f"Канал: {safe_title} (ID канала Telegram: {channel_id})")
    print(f"Логических публикаций: {len(posts)}")
    for post in posts:
        flags = []
        if post.grouped_id is not None:
            flags.append(f"album={post.grouped_id}")
        if post.is_forward:
            flags.append("forwarded")
        suffix = f" {' '.join(flags)}" if flags else ""
        print(
            f"[{post.published_at.isoformat()} message_id={post.message_id} "
            f"{post.external_id}{suffix}]"
        )
        print(post.text if post.text else "[публикация без текста]")
        print()
    if posts:
        print(
            f"Наибольший возвращённый message_id: {max(post.message_id for post in posts)} "
            "(не сохранён; курсором управляет вызывающий код)."
        )


async def _run(args: argparse.Namespace) -> int:
    title, channel_id, posts = await collect(
        args.source,
        after_message_id=args.after_message_id,
        limit=args.limit,
    )
    _print_posts(title, channel_id, posts)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.limit is not None and args.limit <= 0:
        parser.error("--limit должен быть положительным числом")
    if args.after_message_id is not None and args.after_message_id < 0:
        parser.error("--after-message-id не может быть отрицательным")
    try:
        return asyncio.run(_run(args))
    except TelegramConnectorError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except Exception:
        print(
            "Не удалось получить публикации Telegram; технические подробности скрыты.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
