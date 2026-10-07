import argparse

from app.connectors.reddit.client import RedditClient
from app.connectors.reddit.errors import RedditError


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Предварительный просмотр публикаций Reddit без сохранения"
    )
    parser.add_argument("subreddit", help="Имя subreddit")
    parser.add_argument("--limit", type=int, default=10, help="Число последних публикаций")
    args = parser.parse_args()
    if args.limit < 1:
        parser.error("--limit должен быть положительным")
    try:
        with RedditClient() as client:
            posts = client.fetch_latest(args.subreddit, args.limit)
    except (RedditError, ValueError) as exc:
        parser.exit(1, f"Ошибка получения Reddit: {exc}\n")
    for post in reversed(posts):
        print(f"[{post.created_utc.isoformat()}] {post.title}\n{post.permalink}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
