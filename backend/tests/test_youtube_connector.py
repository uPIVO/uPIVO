from datetime import datetime
from pathlib import Path

import httpx
import pytest

from app.connectors.base import FetchResult, InvalidSourceError, RawPublication, SourceFetchError
from app.connectors.youtube import YouTubeConnector
from app.connectors.youtube.__main__ import main
from app.connectors.youtube.persist import persist_fetch
from app.connectors.youtube.urls import parse_channel_url
from app.models.enums import ProcessingStatus, SourceType
from app.models.feed_item import FeedItem
from app.models.source import Source

FIXTURES = Path(__file__).parent / "fixtures" / "youtube"
CHANNEL_ID = "UC_x5XG1OV2P6uZZ5FSM9Ttw"
CHANNEL_RSS = (FIXTURES / "channel.rss.xml").read_text()
EMPTY_RSS = (FIXTURES / "empty.rss.xml").read_text()
CHANNEL_HTML = (FIXTURES / "channel.html").read_text()


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)


def test_source_type_includes_youtube_alongside_telegram_and_habr():
    assert SourceType.YOUTUBE.value == "youtube"
    assert {item.value for item in SourceType} == {
        "telegram",
        "website",
        "rss",
        "habr",
        "youtube",
    }


@pytest.mark.parametrize(
    "url",
    [
        f"https://www.youtube.com/channel/{CHANNEL_ID}",
        f"https://www.youtube.com/channel/{CHANNEL_ID}/videos",
        "https://www.youtube.com/@GoogleDevelopers",
        "https://www.youtube.com/@GoogleDevelopers/about",
        "https://m.youtube.com/@GoogleDevelopers",
        "https://www.youtube.com/c/GoogleDevelopers",
        "https://www.youtube.com/user/GoogleDevelopers",
        "youtube.com/@GoogleDevelopers",
    ],
)
def test_parse_accepts_public_channel_urls(url):
    parsed = parse_channel_url(url)
    assert parsed.lookup_url.startswith("https://www.youtube.com/")


@pytest.mark.parametrize(
    "url",
    [
        "",
        "https://example.com/@GoogleDevelopers",
        "https://youtu.be/dQw4w9WgXcQ",
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://www.youtube.com/playlist?list=PLxxxxxxxx",
        "https://www.youtube.com/results?search_query=python",
        "https://www.youtube.com/shorts/8Xv5BLnXzgI",
        "https://www.youtube.com/feed/subscriptions",
    ],
)
def test_parse_rejects_non_channel_urls(url):
    with pytest.raises(InvalidSourceError):
        parse_channel_url(url)


def test_fetch_recent_videos_from_channel_id():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params.get("channel_id") == CHANNEL_ID
        return httpx.Response(200, text=CHANNEL_RSS)

    result = YouTubeConnector(client=_client(handler)).fetch(
        f"https://www.youtube.com/channel/{CHANNEL_ID}"
    )
    assert result.source_external_id == CHANNEL_ID
    assert result.source_name == "Google for Developers"
    assert result.canonical_url == f"https://www.youtube.com/channel/{CHANNEL_ID}"
    assert len(result.items) == 2
    first = result.items[0]
    assert first.external_id == "8Xv5BLnXzgI"
    assert first.title == "Building a voice-to-print mirror with Gemini"
    assert first.original_url == "https://www.youtube.com/watch?v=8Xv5BLnXzgI"
    assert first.published_at is not None
    assert first.published_at.year == 2026
    assert "Gemini" in first.body_text
    assert first.raw_payload["kind"] == "youtube_rss_entry"
    assert first.raw_payload["channel_id"] == CHANNEL_ID


def test_fetch_resolves_handle_via_channel_page():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/@GoogleDevelopers":
            return httpx.Response(200, text=CHANNEL_HTML)
        if request.url.path.endswith("/feeds/videos.xml"):
            return httpx.Response(200, text=CHANNEL_RSS)
        raise AssertionError(request.url)

    result = YouTubeConnector(client=_client(handler)).fetch(
        "https://www.youtube.com/@GoogleDevelopers"
    )
    assert result.source_external_id == CHANNEL_ID
    assert len(result.items) == 2


def test_fetch_empty_channel_returns_no_items():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=EMPTY_RSS)

    result = YouTubeConnector(client=_client(handler)).fetch(
        f"https://www.youtube.com/channel/{CHANNEL_ID}"
    )
    assert result.items == []
    assert result.source_name == "Empty Test Channel"


def test_fetch_limit_keeps_most_recent_rss_entries():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=CHANNEL_RSS)

    result = YouTubeConnector(client=_client(handler)).fetch(
        f"https://www.youtube.com/channel/{CHANNEL_ID}",
        limit=1,
    )
    assert [item.external_id for item in result.items] == ["8Xv5BLnXzgI"]


def test_fetch_bad_channel_url_does_not_hit_network():
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("network should not be used for an invalid URL")

    with pytest.raises(InvalidSourceError):
        YouTubeConnector(client=_client(handler)).fetch(
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        )


def test_fetch_unresolvable_handle():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html><title>unknown</title></html>")

    with pytest.raises(InvalidSourceError, match="Could not resolve"):
        YouTubeConnector(client=_client(handler)).fetch("https://www.youtube.com/@Nope")


def test_fetch_unknown_channel_http_404():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="not found")

    with pytest.raises(InvalidSourceError):
        YouTubeConnector(client=_client(handler)).fetch(
            f"https://www.youtube.com/channel/{CHANNEL_ID}"
        )


def test_fetch_network_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    with pytest.raises(SourceFetchError, match="Network error"):
        YouTubeConnector(client=_client(handler)).fetch(
            f"https://www.youtube.com/channel/{CHANNEL_ID}"
        )


def test_fetch_timeout():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("timed out")

    with pytest.raises(SourceFetchError, match="Timed out"):
        YouTubeConnector(client=_client(handler)).fetch(
            f"https://www.youtube.com/channel/{CHANNEL_ID}"
        )


def test_cli_bad_url_exits_2():
    assert main(["https://www.youtube.com/watch?v=dQw4w9WgXcQ"]) == 2


def test_cli_network_error_exits_1(monkeypatch):
    def boom(self, url, *, limit=None):
        raise SourceFetchError("Network error while reading YouTube")

    monkeypatch.setattr(YouTubeConnector, "fetch", boom)
    assert main(["https://www.youtube.com/@GoogleDevelopers"]) == 1


def test_cli_dry_run_prints_without_database(capsys, monkeypatch):
    def fake_fetch(self, url, *, limit=None):
        return FetchResult(
            source_name="Demo",
            source_external_id=CHANNEL_ID,
            canonical_url=f"https://www.youtube.com/channel/{CHANNEL_ID}",
            items=[
                RawPublication(
                    external_id="abc",
                    original_url="https://www.youtube.com/watch?v=abc",
                    title="Demo video",
                    body_text="hello",
                    published_at=None,
                )
            ],
        )

    monkeypatch.setattr(YouTubeConnector, "fetch", fake_fetch)
    assert main(["--dry-run", "https://www.youtube.com/@GoogleDevelopers"]) == 0
    output = capsys.readouterr().out
    assert "dry-run" in output
    assert "Demo video" in output
    assert "watch?v=abc" in output


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)


class _FakeSession:
    def __init__(self, source=None, items=None):
        self.source = source
        self.items = list(items or [])

    def scalar(self, stmt):
        return self.source

    def scalars(self, stmt):
        return _Rows(self.items)

    def add(self, obj):
        if isinstance(obj, Source):
            obj.id = 7
            self.source = obj
        elif isinstance(obj, FeedItem):
            self.items.append(obj)

    def flush(self):
        if self.source is not None and self.source.id is None:
            self.source.id = 7


def test_persist_inserts_source_and_raw_feed_items():
    session = _FakeSession()
    fetch = FetchResult(
        source_name="Google for Developers",
        source_external_id=CHANNEL_ID,
        canonical_url=f"https://www.youtube.com/channel/{CHANNEL_ID}",
        items=[
            RawPublication(
                external_id="abc",
                original_url="https://www.youtube.com/watch?v=abc",
                title="Demo video",
                body_text="description",
                published_at=datetime(2026, 10, 1, 19, 0, 13),
                author="Google for Developers",
                raw_payload={"kind": "youtube_rss_entry", "video_id": "abc"},
            )
        ],
    )
    stats = persist_fetch(session, fetch, now=datetime(2026, 10, 5, 12, 0, 0))
    assert stats.source_id == 7
    assert stats.inserted == 1
    assert stats.updated == 0
    assert session.source.type is SourceType.YOUTUBE
    assert session.source.external_id == CHANNEL_ID
    item = session.items[0]
    assert item.source_id == 7
    assert item.title == "Demo video"
    assert item.original_url == "https://www.youtube.com/watch?v=abc"
    assert item.body_text == "description"
    assert item.published_at.year == 2026
    assert item.raw_payload["kind"] == "youtube_rss_entry"


def test_persist_updates_existing_video_by_external_id():
    source = Source(
        type=SourceType.YOUTUBE,
        name="Old",
        canonical_url=f"https://www.youtube.com/channel/{CHANNEL_ID}",
        external_id=CHANNEL_ID,
    )
    source.id = 7
    existing = FeedItem(
        source_id=7,
        external_id="abc",
        title="Old title",
        body_text="old",
        original_url="https://www.youtube.com/watch?v=abc",
        processing_status=ProcessingStatus.PENDING,
    )
    session = _FakeSession(source=source, items=[existing])
    fetch = FetchResult(
        source_name="Google for Developers",
        source_external_id=CHANNEL_ID,
        canonical_url=f"https://www.youtube.com/channel/{CHANNEL_ID}",
        items=[
            RawPublication(
                external_id="abc",
                original_url="https://www.youtube.com/watch?v=abc",
                title="New title",
                body_text="new body",
                published_at=None,
            )
        ],
    )
    stats = persist_fetch(session, fetch, now=datetime(2026, 10, 5, 12, 0, 0))
    assert stats.inserted == 0
    assert stats.updated == 1
    assert existing.title == "New title"
    assert existing.body_text == "new body"
    assert session.source.name == "Google for Developers"
