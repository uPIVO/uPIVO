"""Fetch recent videos from a public YouTube channel RSS feed (no API key)."""

from __future__ import annotations

import calendar
from datetime import UTC, datetime
from typing import Any

import feedparser
import httpx
from sqlalchemy.orm import Session

from app.connectors.base import (
    FetchResult,
    InvalidSourceError,
    RawPublication,
    SourceFetchError,
)
from app.connectors.youtube.captions import fetch_transcript
from app.connectors.youtube.persist import StoredTranscript, load_existing_transcripts
from app.connectors.youtube.urls import (
    ParsedChannelUrl,
    canonical_channel_url,
    extract_channel_id,
    parse_channel_url,
    rss_url,
)
from app.models.enums import SourceType

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}
DEFAULT_TIMEOUT = 20.0
DEFAULT_LIMIT = 15


class YouTubeConnector:
    """SourceConnector for public YouTube channels via channel RSS."""

    source_type = SourceType.YOUTUBE

    def __init__(
        self,
        client: httpx.Client | None = None,
        *,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self._client = client
        self._timeout = timeout

    def fetch(
        self,
        canonical_url: str,
        *,
        limit: int | None = DEFAULT_LIMIT,
        session: Session | None = None,
    ) -> FetchResult:
        parsed = parse_channel_url(canonical_url)
        if limit is not None and limit < 1:
            raise InvalidSourceError("limit must be >= 1")
        if self._client is not None:
            return self._fetch_with(self._client, parsed, limit, session)
        with httpx.Client(
            timeout=self._timeout,
            follow_redirects=True,
            headers=DEFAULT_HEADERS,
        ) as client:
            return self._fetch_with(client, parsed, limit, session)

    def _fetch_with(
        self,
        client: httpx.Client,
        parsed: ParsedChannelUrl,
        limit: int | None,
        session: Session | None,
    ) -> FetchResult:
        channel_id = parsed.channel_id or self._resolve_channel_id(client, parsed.lookup_url)
        feed = self._download_rss(client, rss_url(channel_id))
        source_name = str(feed.feed.get("title") or "").strip() or channel_id
        items = [
            item
            for entry in feed.entries
            if (item := _entry_to_publication(entry, channel_id)) is not None
        ]
        if limit is not None:
            items = items[:limit]
        stored = (
            load_existing_transcripts(session, channel_id, [item.external_id for item in items])
            if session is not None
            else {}
        )
        items = [_with_transcript(client, item, stored.get(item.external_id)) for item in items]
        return FetchResult(
            source_name=source_name,
            source_external_id=channel_id,
            canonical_url=canonical_channel_url(channel_id),
            items=items,
        )

    def _resolve_channel_id(self, client: httpx.Client, lookup_url: str) -> str:
        response = _get(client, lookup_url)
        channel_id = extract_channel_id(response.text, str(response.url))
        if not channel_id:
            raise InvalidSourceError(
                "Could not resolve a public YouTube channel id from the given URL"
            )
        return channel_id

    def _download_rss(self, client: httpx.Client, url: str) -> feedparser.FeedParserDict:
        response = _get(client, url)
        parsed = feedparser.parse(response.content)
        has_feed = bool(parsed.feed)
        if parsed.bozo and not parsed.entries and not has_feed:
            detail = getattr(parsed, "bozo_exception", None)
            raise SourceFetchError(f"YouTube RSS feed could not be parsed: {detail}") from None
        return parsed


def _get(client: httpx.Client, url: str) -> httpx.Response:
    try:
        response = client.get(url)
        response.raise_for_status()
        return response
    except httpx.TimeoutException as exc:
        raise SourceFetchError("Timed out while reading YouTube") from exc
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        if status in {400, 404, 410}:
            raise InvalidSourceError(
                "YouTube channel was not found or the URL is not a public channel"
            ) from exc
        raise SourceFetchError(f"YouTube returned HTTP {status}") from exc
    except httpx.HTTPError as exc:
        raise SourceFetchError("Network error while reading YouTube") from exc


def _entry_to_publication(entry: Any, channel_id: str) -> RawPublication | None:
    video_id = _video_id(entry)
    if not video_id:
        return None
    title = _text(entry.get("title"))
    description = _media_description(entry)
    link = _text(entry.get("link")) or f"https://www.youtube.com/watch?v={video_id}"
    author = _text(entry.get("author"))
    published_at = _published_at(entry)
    payload = {
        "kind": "youtube_rss_entry",
        "video_id": video_id,
        "channel_id": channel_id,
        "title": title,
        "link": link,
        "published": entry.get("published"),
        "author": author,
        "description": description,
        "thumbnail_url": _thumbnail_url(entry),
    }
    return RawPublication(
        external_id=video_id,
        original_url=link,
        title=title,
        body_text=description,
        published_at=published_at,
        author=author,
        raw_payload=payload,
    )


def _with_transcript(
    client: httpx.Client,
    item: RawPublication,
    stored: StoredTranscript | None = None,
) -> RawPublication:
    if stored is not None:
        payload = dict(item.raw_payload)
        payload["transcript"] = stored.text
        if stored.lang:
            payload["transcript_lang"] = stored.lang
        if stored.kind:
            payload["transcript_kind"] = stored.kind
        return RawPublication(
            external_id=item.external_id,
            original_url=item.original_url,
            title=item.title,
            body_text=stored.text,
            published_at=item.published_at,
            author=item.author,
            raw_payload=payload,
        )
    caption = fetch_transcript(client, item.external_id)
    payload = dict(item.raw_payload)
    if caption is None:
        payload.setdefault("transcript", None)
        payload.setdefault("transcript_lang", None)
        return RawPublication(
            external_id=item.external_id,
            original_url=item.original_url,
            title=item.title,
            body_text=item.body_text,
            published_at=item.published_at,
            author=item.author,
            raw_payload=payload,
        )
    payload["transcript"] = caption.text
    payload["transcript_lang"] = caption.lang
    payload["transcript_kind"] = caption.kind
    return RawPublication(
        external_id=item.external_id,
        original_url=item.original_url,
        title=item.title,
        body_text=caption.text,
        published_at=item.published_at,
        author=item.author,
        raw_payload=payload,
    )


def _video_id(entry: Any) -> str | None:
    video_id = _text(entry.get("yt_videoid"))
    if video_id:
        return video_id
    entry_id = _text(entry.get("id"))
    if entry_id and entry_id.startswith("yt:video:"):
        return entry_id.rsplit(":", 1)[-1]
    return None


def _media_description(entry: Any) -> str:
    media = entry.get("media_description")
    if isinstance(media, str) and media.strip():
        return media
    if isinstance(media, list) and media:
        first = media[0]
        if isinstance(first, dict):
            return str(first.get("content") or first.get("#text") or "")
        content = getattr(first, "content", None)
        if isinstance(content, str):
            return content
    return _text(entry.get("summary")) or ""


def _thumbnail_url(entry: Any) -> str | None:
    thumbs = entry.get("media_thumbnail") or []
    if not thumbs:
        return None
    first = thumbs[0]
    if isinstance(first, dict):
        url = first.get("url")
        return str(url) if url else None
    url = getattr(first, "url", None)
    return str(url) if url else None


def _published_at(entry: Any) -> datetime | None:
    parsed = entry.get("published_parsed")
    if not parsed:
        return None
    try:
        return datetime.fromtimestamp(calendar.timegm(parsed), UTC).replace(tzinfo=None)
    except (OverflowError, TypeError, ValueError, OSError):
        return None


def _text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None
