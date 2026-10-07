import json
import re
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from app.connectors.reddit.errors import RedditRequestError, RedditSaturatedWindowError
from app.connectors.reddit.models import RedditPost


def _parse_post(row: dict[str, Any]) -> RedditPost:
    try:
        return RedditPost.from_payload(row)
    except (KeyError, TypeError, ValueError, OverflowError, OSError) as exc:
        raise RedditRequestError(f"Arctic Shift вернул некорректную публикацию: {exc}") from exc


def _error_detail(body: bytes) -> str:
    try:
        value = json.loads(body)
        parsed_json = True
    except (ValueError, UnicodeDecodeError):
        value = body.decode("utf-8", "replace")
        parsed_json = False

    def extract(item: Any) -> str:
        if isinstance(item, str):
            return item
        if isinstance(item, list):
            parts = [extract(entry) for entry in item]
            return "; ".join(part for part in parts if part)
        if isinstance(item, dict):
            for key in ("detail", "message", "error", "msg"):
                if key in item:
                    result = extract(item[key])
                    if result:
                        return result
        return ""

    text = extract(value)
    if not text:
        if parsed_json:
            return ""
        text = body.decode("utf-8", "replace")
    text = "".join(" " if ord(char) < 32 or ord(char) == 127 else char for char in text)
    text = re.sub(
        r"(?i)(authorization[ \t]*[=:][ \t]*)(?:bearer[ \t]+)?[^ \t,;]+",
        r"\1[скрыто]",
        text,
    )
    text = re.sub(
        r"(?i)(password|passwd|token|authorization|secret|api[_-]?key)[ \t]*([=:])[ \t]*[^ \t,;]+",
        r"\1\2[скрыто]",
        text,
    )
    text = re.sub(r"https?://[^ \"']+", "[URL]", text)
    return " ".join(text.split())[:300]


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
        max_response_bytes: int = 5 * 1024 * 1024,
        http_client: httpx.Client | None = None,
    ) -> None:
        self.timeout = timeout
        self.deadline_seconds = deadline_seconds
        self.max_requests = max_requests
        self.max_responses = max_responses
        self.max_response_bytes = max_response_bytes
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

    def _read_bounded_body(self, response: httpx.Response) -> tuple[bytes | None, str | None]:
        content_length = response.headers.get("content-length")
        if content_length is not None:
            try:
                if int(content_length) > self.max_response_bytes:
                    return None, "размер тела ответа превышает установленный предел"
            except ValueError:
                pass
        chunks: list[bytes] = []
        size = 0
        for chunk in response.iter_bytes():
            if time.monotonic() - self._started >= self.deadline_seconds:
                return None, "истёк срок чтения ответа"
            size += len(chunk)
            if size > self.max_response_bytes:
                return None, "размер тела ответа превышает установленный предел"
            chunks.append(chunk)
        if time.monotonic() - self._started >= self.deadline_seconds:
            return None, "истёк срок чтения ответа"
        return b"".join(chunks), None

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
        remaining = self.deadline_seconds - (time.monotonic() - self._started)
        if remaining <= 0:
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
            with self.http_client.stream(
                "GET", self.URL, params=params, timeout=min(self.timeout, remaining)
            ) as response:
                self._responses += 1
                status = response.status_code
                if not response.is_success:
                    try:
                        body, read_error = self._read_bounded_body(response)
                    except (httpx.HTTPError, ValueError):
                        body, read_error = None, "тело ошибки не удалось прочитать"
                    detail = _error_detail(body) if body else ""
                    context = detail or read_error
                    suffix = f": {context}" if context else ""
                    raise RedditRequestError(f"Arctic Shift вернул HTTP {status}{suffix}")
                body, read_error = self._read_bounded_body(response)
                if read_error == "истёк срок чтения ответа":
                    raise RedditRequestError("Превышен срок получения данных Reddit")
                if read_error:
                    raise RedditRequestError("Ответ Arctic Shift превышает допустимый размер")
            data = json.loads(body or b"")
        except RedditRequestError:
            raise
        except httpx.HTTPError as exc:
            raise RedditRequestError(
                f"Не удалось выполнить запрос к Arctic Shift ({type(exc).__name__})"
            ) from exc
        except ValueError as exc:
            raise RedditRequestError("Arctic Shift вернул некорректный JSON") from exc
        if not isinstance(data, dict) or not isinstance(data.get("data"), list):
            raise RedditRequestError("Arctic Shift вернул ответ без массива data")
        rows = data["data"]
        if any(not isinstance(item, dict) for item in rows):
            raise RedditRequestError("Arctic Shift вернул публикацию неожиданного формата")
        return rows

    def fetch_latest(self, subreddit: str, limit: int = 50) -> list[RedditPost]:
        if limit < 1:
            raise ValueError("Лимит должен быть положительным")
        batch = self._search(subreddit, sort="desc", limit=min(self.PAGE_SIZE, limit))
        posts = {post.id: post for post in map(_parse_post, batch)}
        if len(batch) < self.PAGE_SIZE or len(posts) >= limit:
            return sorted(
                posts.values(), key=lambda post: (post.created_utc, post.id), reverse=True
            )[:limit]

        # Без offset продолжаем назад временными окнами с перекрытием; бюджет запроса ограничен.
        cursor = min(post.created_utc for post in posts.values())
        history_start = datetime(2005, 1, 1)
        span = timedelta(seconds=1)
        while len(posts) < limit and cursor > history_start:
            start = max(cursor - span, history_start)
            older = self.fetch_range(subreddit, start, cursor + timedelta(microseconds=1))
            posts.update((post.id, post) for post in older)
            cursor = start
            span *= 2
        if len(posts) < limit and self._requests >= self.max_requests:
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
                    post = _parse_post(row)
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
