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
CAPTIONS_LIST = (FIXTURES / "captions-list.xml").read_text()
CAPTIONS_EN = (FIXTURES / "captions-en.xml").read_text()
CAPTIONS_LIST_EMPTY = (FIXTURES / "captions-list-empty.xml").read_text()


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)


def _rss_handler(
    rss: str = CHANNEL_RSS,
    *,
    list_xml: str = CAPTIONS_LIST_EMPTY,
    transcript_xml: str | None = None,
    caption_error: Exception | None = None,
):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/feeds/videos.xml"):
            return httpx.Response(200, text=rss)
        if path == "/@GoogleDevelopers":
            return httpx.Response(200, text=CHANNEL_HTML)
        if path == "/api/timedtext":
            if caption_error is not None:
                raise caption_error
            if request.url.params.get("type") == "list":
                return httpx.Response(200, text=list_xml)
            if transcript_xml:
                return httpx.Response(200, text=transcript_xml)
            return httpx.Response(200, content=b"")
        if path == "/watch":
            if caption_error is not None:
                raise caption_error
            return httpx.Response(200, text="<html><title>no player captions</title></html>")
        raise AssertionError(request.url)

    return handler


def test_source_type_includes_youtube_alongside_telegram_and_habr():
    assert SourceType.YOUTUBE.value == "youtube"
    assert {"telegram", "website", "rss", "habr", "youtube"} <= {
        item.value for item in SourceType
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
    result = YouTubeConnector(client=_client(_rss_handler())).fetch(
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
    result = YouTubeConnector(client=_client(_rss_handler())).fetch(
        "https://www.youtube.com/@GoogleDevelopers"
    )
    assert result.source_external_id == CHANNEL_ID
    assert len(result.items) == 2


def test_fetch_empty_channel_returns_no_items():
    result = YouTubeConnector(client=_client(_rss_handler(EMPTY_RSS))).fetch(
        f"https://www.youtube.com/channel/{CHANNEL_ID}"
    )
    assert result.items == []
    assert result.source_name == "Empty Test Channel"


def test_fetch_limit_keeps_most_recent_rss_entries():
    result = YouTubeConnector(client=_client(_rss_handler())).fetch(
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


def test_fetch_uses_captions_as_body_text_when_present():
    result = YouTubeConnector(
        client=_client(_rss_handler(list_xml=CAPTIONS_LIST, transcript_xml=CAPTIONS_EN))
    ).fetch(f"https://www.youtube.com/channel/{CHANNEL_ID}", limit=1)
    item = result.items[0]
    assert item.body_text == "Hello from the transcript.\nThis is caption line two."
    assert item.raw_payload["description"].startswith("Gayatri")
    assert item.raw_payload["transcript"] == item.body_text
    assert item.raw_payload["transcript_lang"] == "en"
    assert item.title == "Building a voice-to-print mirror with Gemini"
    assert item.original_url.startswith("https://www.youtube.com/")
    assert item.published_at is not None


def test_fetch_keeps_video_when_captions_are_missing():
    result = YouTubeConnector(client=_client(_rss_handler())).fetch(
        f"https://www.youtube.com/channel/{CHANNEL_ID}",
        limit=1,
    )
    item = result.items[0]
    assert "Gemini" in item.body_text
    assert item.raw_payload.get("transcript") is None


def test_fetch_keeps_video_when_caption_request_fails():
    result = YouTubeConnector(
        client=_client(_rss_handler(caption_error=httpx.ConnectError("captions down")))
    ).fetch(f"https://www.youtube.com/channel/{CHANNEL_ID}", limit=1)
    item = result.items[0]
    assert item.external_id == "8Xv5BLnXzgI"
    assert "Gemini" in item.body_text
    assert item.raw_payload.get("transcript") is None


def test_cli_bad_url_exits_2():
    assert main(["https://www.youtube.com/watch?v=dQw4w9WgXcQ"]) == 2


def test_cli_network_error_exits_1(monkeypatch):
    def boom(self, url, *, limit=None, session=None):
        raise SourceFetchError("Network error while reading YouTube")

    monkeypatch.setattr(YouTubeConnector, "fetch", boom)
    assert main(["https://www.youtube.com/@GoogleDevelopers"]) == 1


def test_cli_dry_run_prints_without_database(capsys, monkeypatch):
    def fake_fetch(self, url, *, limit=None, session=None):
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
    assert "captions=no" in output


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


def test_persist_inserts_source_and_raw_feed_items(monkeypatch):
    session = _FakeSession()
    def fake_upsert(given_session, fetch, stamp):
        source = Source(
            type=SourceType.YOUTUBE,
            name=fetch.source_name,
            canonical_url=fetch.canonical_url,
            external_id=fetch.source_external_id,
            last_synced_at=stamp,
        )
        given_session.add(source)
        return source

    monkeypatch.setattr("app.connectors.youtube.persist._upsert_source", fake_upsert)
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
    stats = persist_fetch(session, fetch, now=datetime(2026, 10, 5, 12, 0, 0), source=source)
    assert stats.inserted == 0
    assert stats.updated == 1
    assert existing.title == "New title"
    assert existing.body_text == "new body"
    assert session.source.name == "Google for Developers"


def _youtube_source():
    source = Source(
        type=SourceType.YOUTUBE,
        name="Google for Developers",
        canonical_url=f"https://www.youtube.com/channel/{CHANNEL_ID}",
        external_id=CHANNEL_ID,
    )
    source.id = 7
    return source


def test_skips_caption_http_when_transcript_already_stored():
    caption_calls = []
    inner = _rss_handler(list_xml=CAPTIONS_LIST, transcript_xml=CAPTIONS_EN)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path in {"/api/timedtext", "/watch"}:
            caption_calls.append(request.url.path)
        return inner(request)

    existing = FeedItem(
        source_id=7,
        external_id="8Xv5BLnXzgI",
        title="Old title",
        body_text="already stored transcript",
        original_url="https://www.youtube.com/watch?v=8Xv5BLnXzgI",
        raw_payload={
            "transcript": "already stored transcript",
            "transcript_lang": "en",
            "description": "old description",
        },
        processing_status=ProcessingStatus.PENDING,
    )
    result = YouTubeConnector(client=_client(handler)).fetch(
        f"https://www.youtube.com/channel/{CHANNEL_ID}",
        limit=1,
        session=_FakeSession(source=_youtube_source(), items=[existing]),
    )
    assert caption_calls == []
    item = result.items[0]
    assert item.body_text == "already stored transcript"
    assert item.title == "Building a voice-to-print mirror with Gemini"
    assert item.raw_payload["transcript"] == "already stored transcript"
    assert item.raw_payload["description"].startswith("Gayatri")
    assert item.original_url.startswith("https://www.youtube.com/")


def test_fetches_captions_when_stored_row_has_no_transcript():
    caption_calls = []
    inner = _rss_handler(list_xml=CAPTIONS_LIST, transcript_xml=CAPTIONS_EN)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/timedtext":
            caption_calls.append(request.url.path)
        return inner(request)

    existing = FeedItem(
        source_id=7,
        external_id="8Xv5BLnXzgI",
        title="Old title",
        body_text="Gayatri built a custom journaling device using Gemini.",
        original_url="https://www.youtube.com/watch?v=8Xv5BLnXzgI",
        raw_payload={
            "description": "Gayatri built a custom journaling device using Gemini.",
            "transcript": None,
        },
        processing_status=ProcessingStatus.PENDING,
    )
    result = YouTubeConnector(client=_client(handler)).fetch(
        f"https://www.youtube.com/channel/{CHANNEL_ID}",
        limit=1,
        session=_FakeSession(source=_youtube_source(), items=[existing]),
    )
    assert caption_calls
    item = result.items[0]
    assert item.body_text == "Hello from the transcript.\nThis is caption line two."
    assert item.raw_payload["transcript"] == item.body_text


def test_persist_keeps_existing_transcript_when_incoming_has_none():
    source = _youtube_source()
    existing = FeedItem(
        source_id=7,
        external_id="abc",
        title="Old title",
        body_text="already stored transcript",
        original_url="https://www.youtube.com/watch?v=abc",
        raw_payload={
            "transcript": "already stored transcript",
            "transcript_lang": "en",
            "description": "old description",
        },
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
                body_text="rss description only",
                published_at=None,
                raw_payload={"kind": "youtube_rss_entry", "description": "rss description only"},
            )
        ],
    )
    persist_fetch(session, fetch, now=datetime(2026, 10, 5, 12, 0, 0), source=source)
    assert existing.title == "New title"
    assert existing.body_text == "already stored transcript"
    assert existing.raw_payload["transcript"] == "already stored transcript"
    assert existing.raw_payload["description"] == "rss description only"
