import json
import re
import time
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from app.connectors.reddit.errors import RedditRequestError, RedditSaturatedWindowError
from app.connectors.reddit.models import RedditFetchBatch, RedditPost


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


def _format_api_date(value: datetime, *, upper_bound: bool) -> str:
    """Сериализовать UTC-границу с консервативным округлением до миллисекунды."""
    utc = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    milliseconds, remainder = divmod(utc.microsecond, 1000)
    if upper_bound and remainder:
        milliseconds += 1
        if milliseconds == 1000:
            utc = utc.replace(microsecond=0) + timedelta(seconds=1)
            milliseconds = 0
    rounded = utc.replace(microsecond=milliseconds * 1000)
    return rounded.isoformat(timespec="milliseconds").replace("+00:00", "Z")


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
            params["after"] = _format_api_date(after, upper_bound=False)
        if before is not None:
            params["before"] = _format_api_date(before, upper_bound=True)
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

    def fetch_latest(
        self, subreddit: str, limit: int = 50, *, as_of: datetime | None = None
    ) -> RedditFetchBatch:
        if limit < 1:
            raise ValueError("Лимит должен быть положительным")

        eligible: dict[str, RedditPost] = {}
        scanned_ids: set[str] = set()
        scanned_through: datetime | None = None
        pagination_cursor: datetime | None = None
        inspected_count = 0

        def inspect(rows: Sequence[dict[str, Any] | RedditPost]) -> None:
            nonlocal scanned_through, pagination_cursor, inspected_count
            for row in rows:
                post = row if isinstance(row, RedditPost) else _parse_post(row)
                if post.id in scanned_ids:
                    continue
                scanned_ids.add(post.id)
                inspected_count += 1
                if pagination_cursor is None or post.created_utc < pagination_cursor:
                    pagination_cursor = post.created_utc
                if as_of is not None and post.created_utc > as_of:
                    continue
                if scanned_through is None or post.created_utc > scanned_through:
                    scanned_through = post.created_utc
                if not post.is_archived_removed:
                    eligible[post.id] = post

        first_page = self._search(subreddit, sort="desc", limit=self.PAGE_SIZE, before=as_of)
        inspect(first_page)
        history_start = datetime(2005, 1, 1)
        span = timedelta(seconds=1)
        while (
            len(eligible) < limit
            and len(first_page) == self.PAGE_SIZE
            and pagination_cursor is not None
            and pagination_cursor > history_start
        ):
            start = max(pagination_cursor - span, history_start)
            older = self.fetch_range(
                subreddit, start, pagination_cursor + timedelta(microseconds=1)
            )
            inspect(older)
            pagination_cursor = start
            span *= 2
        if len(eligible) < limit and pagination_cursor and pagination_cursor > history_start:
            if self._requests >= self.max_requests:
                raise RedditRequestError("Лимита запросов не хватило для начальной выборки Reddit")
        selected = sorted(
            eligible.values(), key=lambda post: (post.created_utc, post.id), reverse=True
        )[:limit]
        return RedditFetchBatch(selected, scanned_through, inspected_count)

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
