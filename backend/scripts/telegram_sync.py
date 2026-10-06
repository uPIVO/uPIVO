import argparse
import asyncio
import sys
from collections.abc import Sequence

from app.connectors.telegram.client import TelegramConnector
from app.connectors.telegram.errors import TelegramConnectorError
from app.connectors.telegram.models import TelegramPost


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read Telegram channel posts to the console.")
    parser.add_argument("source", help="username, channel t.me URL, or signed channel ID")
    parser.add_argument(
        "--after-message-id",
        type=int,
        help="fetch all posts newer than this caller-supplied Telegram message ID",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=50,
        help="initial latest logical posts (default: 50; only applies without --after-message-id)",
    )
    return parser


async def collect(
    source: str, *, after_message_id: int | None, limit: int
) -> tuple[str, int, list[TelegramPost]]:
    async with TelegramConnector() as connector:
        channel = await connector.resolve_channel(source)
        if after_message_id is None:
            posts = await connector.fetch_latest_posts(channel, limit=limit)
        else:
            posts = await connector.fetch_new_posts(channel, after_message_id=after_message_id)
    return channel.title, channel.channel_id, posts


def _print_posts(title: str, channel_id: int, posts: list[TelegramPost]) -> None:
    safe_title = " ".join(title.split()) or "(untitled channel)"
    print(f"Channel: {safe_title} (Telegram channel ID {channel_id})")
    print(f"Logical posts: {len(posts)}")
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
        print(post.text if post.text else "[textless post]")
        print()
    if posts:
        print(
            f"Highest returned message_id: {max(post.message_id for post in posts)} "
            "(not saved; caller owns the cursor)."
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
    if args.limit <= 0:
        parser.error("--limit must be a positive integer")
    if args.after_message_id is not None and args.after_message_id < 0:
        parser.error("--after-message-id must be non-negative")
    try:
        return asyncio.run(_run(args))
    except TelegramConnectorError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except Exception:
        print("Telegram sync failed; details were suppressed.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
