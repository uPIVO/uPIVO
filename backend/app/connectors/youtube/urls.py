"""Parse and validate public YouTube channel URLs."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

from app.connectors.base import InvalidSourceError

YOUTUBE_HOSTS = frozenset(
    {
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "music.youtube.com",
    }
)
CHANNEL_ID_RE = re.compile(r"^UC[\w-]{22}$")
CHANNEL_TABS = frozenset(
    {
        "about",
        "community",
        "courses",
        "featured",
        "live",
        "playlists",
        "podcasts",
        "releases",
        "search",
        "shorts",
        "store",
        "streams",
        "videos",
    }
)
REJECT_FIRST_SEGMENTS = frozenset(
    {
        "clip",
        "embed",
        "feed",
        "hashtag",
        "playlist",
        "redirect",
        "results",
        "shop",
        "watch",
    }
)
RSS_CHANNEL_RE = re.compile(r"feeds/videos\.xml\?channel_id=(UC[\w-]{22})", re.I)
CANONICAL_CHANNEL_RE = re.compile(r"/channel/(UC[\w-]{22})", re.I)
EXTERNAL_ID_RE = re.compile(r'"externalId"\s*:\s*"(UC[\w-]{22})"')


@dataclass(frozen=True)
class ParsedChannelUrl:
    raw: str
    channel_id: str | None
    lookup_url: str


def rss_url(channel_id: str) -> str:
    return f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"


def canonical_channel_url(channel_id: str) -> str:
    return f"https://www.youtube.com/channel/{channel_id}"


def parse_channel_url(url: str) -> ParsedChannelUrl:
    text = (url or "").strip()
    if not text:
        raise InvalidSourceError("YouTube channel URL is empty")

    parsed = urlparse(text if "://" in text else f"https://{text}")
    host = (parsed.hostname or "").lower()
    if host not in YOUTUBE_HOSTS:
        raise InvalidSourceError(
            "Expected a public YouTube channel URL on youtube.com "
            "(not a search, playlist, video, or third-party link)"
        )

    parts = [segment for segment in (parsed.path or "/").split("/") if segment]
    query = parse_qs(parsed.query)
    if not parts:
        raise InvalidSourceError("Expected a public YouTube channel URL, not the YouTube homepage")

    first = parts[0].lower()
    if first in REJECT_FIRST_SEGMENTS or "search_query" in query:
        raise InvalidSourceError(
            "Expected a public YouTube channel URL, not a search, playlist, or video link"
        )
    if first == "shorts" and len(parts) >= 2:
        raise InvalidSourceError("Expected a public YouTube channel URL, not a video link")
    if "list" in query and first != "channel":
        raise InvalidSourceError("Expected a public YouTube channel URL, not a playlist link")

    if len(parts) >= 2 and parts[-1].lower() in CHANNEL_TABS:
        parts = parts[:-1]

    if first == "channel" and len(parts) >= 2:
        channel_id = parts[1]
        if not CHANNEL_ID_RE.fullmatch(channel_id):
            raise InvalidSourceError(f"Invalid YouTube channel id: {channel_id}")
        return ParsedChannelUrl(
            raw=text,
            channel_id=channel_id,
            lookup_url=canonical_channel_url(channel_id),
        )

    if parts[0].startswith("@") and len(parts[0]) > 1:
        handle = parts[0]
        return ParsedChannelUrl(
            raw=text,
            channel_id=None,
            lookup_url=f"https://www.youtube.com/{handle}",
        )

    if first in {"c", "user"} and len(parts) >= 2:
        return ParsedChannelUrl(
            raw=text,
            channel_id=None,
            lookup_url=f"https://www.youtube.com/{first}/{parts[1]}",
        )

    raise InvalidSourceError(
        "Expected a public YouTube channel URL such as "
        "https://www.youtube.com/@handle or /channel/UC..."
    )


def extract_channel_id(html: str, final_url: str | None = None) -> str | None:
    if final_url:
        match = CANONICAL_CHANNEL_RE.search(final_url)
        if match and CHANNEL_ID_RE.fullmatch(match.group(1)):
            return match.group(1)
    for pattern in (RSS_CHANNEL_RE, CANONICAL_CHANNEL_RE, EXTERNAL_ID_RE):
        match = pattern.search(html or "")
        if match and CHANNEL_ID_RE.fullmatch(match.group(1)):
            return match.group(1)
    return None
