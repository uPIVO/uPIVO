import argparse
import sys

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker

from app.connectors.reddit.errors import RedditError
from app.db.session import get_engine
from app.services.reddit import sync_reddit


def main() -> int:
    parser = argparse.ArgumentParser(description="Сохранить публикации Reddit в локальную БД")
    parser.add_argument("subreddit", help="Имя subreddit или URL r/subreddit")
    parser.add_argument("--limit", type=int, help="Максимум новых публикаций за запуск")
    parser.add_argument(
        "--overlap-seconds",
        type=int,
        default=3600,
        help="Период перекрытия для поздно появившихся публикаций",
    )
    args = parser.parse_args()
    if args.limit is not None and args.limit < 1:
        parser.error("--limit должен быть положительным")
    try:
        result = sync_reddit(
            sessionmaker(get_engine()),
            args.subreddit,
            limit=args.limit,
            initial_limit=args.limit or 50,
            overlap_seconds=args.overlap_seconds,
        )
    except (RedditError, SQLAlchemyError, ValueError) as exc:
        print(f"Ошибка сбора Reddit: {exc}", file=sys.stderr)
        return 1
    print(f"r/{result.subreddit}: получено {result.fetched}, сохранено {result.saved}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
