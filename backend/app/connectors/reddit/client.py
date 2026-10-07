import time
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from app.connectors.reddit.errors import RedditRequestError, RedditSaturatedWindowError
from app.connectors.reddit.models import RedditPost


class RedditClient:
    URL = "https://arctic-shift.photon-reddit.com/api/posts/search"
    PAGE_SIZE = 100

    def __init__(
        self,
        *,
        timeout: float = 10,
        deadline_seconds: float = 90,
        max_requests: int = 120,
        max_responses: int = 120,
        http_client: httpx.Client | None = None,
    ) -> None:
        self.timeout = timeout
        self.deadline_seconds = deadline_seconds
        self.max_requests = max_requests
        self.max_responses = max_responses
        self.http_client = http_client or httpx.Client(timeout=timeout)
        self._owns_client = http_client is None
        self._started = 0.0
        self._requests = 0
        self._responses = 0

    def close(self) -> None:
        if self._owns_client:
            self.http_client.close()

    def __enter__(self) -> "RedditClient":
        self._started = time.monotonic()
        self._requests = self._responses = 0
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _search(
        self,
        subreddit: str,
        *,
        sort: str,
        limit: int,
        after: datetime | None = None,
        before: datetime | None = None,
    ) -> list[dict[str, Any]]:
        if self._started == 0:
            self._started = time.monotonic()
        if time.monotonic() - self._started >= self.deadline_seconds:
            raise RedditRequestError("Превышен срок получения данных Reddit")
        if self._requests >= self.max_requests or self._responses >= self.max_responses:
            raise RedditRequestError("Превышен лимит запросов Arctic Shift за запуск")
        params: dict[str, str | int] = {"subreddit": subreddit, "sort": sort, "limit": limit}
        if after is not None:
            params["after"] = after.replace(tzinfo=UTC).isoformat()
        if before is not None:
            params["before"] = before.replace(tzinfo=UTC).isoformat()
        self._requests += 1
        try:
            response = self.http_client.get(self.URL, params=params, timeout=self.timeout)
            self._responses += 1
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise RedditRequestError(f"Не удалось получить данные Arctic Shift: {exc}") from exc
        if not isinstance(data, list) or any(not isinstance(item, dict) for item in data):
            raise RedditRequestError("Arctic Shift вернул ответ неожиданного формата")
        return data

    def fetch_latest(self, subreddit: str, limit: int = 50) -> list[RedditPost]:
        if limit < 1:
            raise ValueError("Лимит должен быть положительным")
        batch = self._search(subreddit, sort="desc", limit=min(self.PAGE_SIZE, limit))
        posts = {post.id: post for post in map(RedditPost.from_payload, batch)}
        if len(batch) < self.PAGE_SIZE or len(posts) >= limit:
            return sorted(
                posts.values(), key=lambda post: (post.created_utc, post.id), reverse=True
            )[:limit]

        # Без offset продолжаем назад временными окнами с перекрытием; бюджет запроса ограничен.
        cursor = min(post.created_utc for post in posts.values())
        span = timedelta(seconds=1)
        while len(posts) < limit:
            start = cursor - span
            older = self.fetch_range(subreddit, start, cursor + timedelta(microseconds=1))
            posts.update((post.id, post) for post in older)
            cursor = start
            span *= 2
            if self._requests >= self.max_requests and len(posts) < limit:
                raise RedditRequestError("Лимита запросов не хватило для начальной выборки Reddit")
        return sorted(posts.values(), key=lambda post: (post.created_utc, post.id), reverse=True)[
            :limit
        ]

    def fetch_range(self, subreddit: str, after: datetime, before: datetime) -> list[RedditPost]:
        """Получить весь диапазон; насыщенные интервалы делятся до секундной точности."""
        if after >= before:
            return []
        gathered: dict[str, RedditPost] = {}
        pending = [(after, before)]
        while pending:
            start, end = pending.pop()
            rows = self._search(
                subreddit, sort="asc", limit=self.PAGE_SIZE, after=start, before=end
            )
            if len(rows) < self.PAGE_SIZE:
                for row in rows:
                    post = RedditPost.from_payload(row)
                    gathered[post.id] = post
                continue
            midpoint = start + (end - start) / 2
            # Перекрытие на границе покрывает оба варианта включительности диапазона.
            if end - start <= timedelta(seconds=1):
                raise RedditSaturatedWindowError(
                    "Более 100 публикаций имеют неразделимый временной штамп; курсор не продвинут"
                )
            pending.append((midpoint - timedelta(microseconds=1), end))
            pending.append((start, midpoint + timedelta(microseconds=1)))
        return sorted(gathered.values(), key=lambda post: (post.created_utc, post.id))
