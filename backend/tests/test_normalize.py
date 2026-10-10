from datetime import datetime
from pathlib import Path

from app.models.enums import ProcessingStatus
from app.models.feed_item import FeedItem
from app.processing.__main__ import main
from app.processing.normalize import (
    content_hash,
    list_pending_feed_items,
    map_raw_payload,
    normalize_feed_item,
    normalize_pending,
)
from app.processing.text import clean_text

FIXTURE = Path(__file__).parent / "fixtures" / "normalize" / "sample.json"

YOUTUBE_PAYLOAD = {
    "kind": "youtube_rss_entry",
    "video_id": "8Xv5BLnXzgI",
    "channel_id": "UC_x5XG1OV2P6uZZ5FSM9Ttw",
    "title": "  Building a voice-to-print mirror with Gemini  ",
    "link": "https://www.youtube.com/watch?v=8Xv5BLnXzgI",
    "published": "2026-10-01T19:00:13+00:00",
    "author": "Google for Developers",
    "description": "<p>Gayatri talks about Gemini.</p>",
    "transcript": "Hello from the transcript.\n\nThis is caption line two.",
}


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)


class _FakeSession:
    def __init__(self, items=None):
        self.items = list(items or [])

    def scalars(self, stmt):
        return _Rows(self.items)


def _item(**kwargs) -> FeedItem:
    values = {
        "source_id": 1,
        "external_id": "abc",
        "title": "Old title",
        "body_text": "old body",
        "original_url": "https://example.com/old",
        "author": "Old",
        "published_at": datetime(2020, 1, 1),
        "raw_payload": dict(YOUTUBE_PAYLOAD),
        "processing_status": ProcessingStatus.PENDING,
        "extracted_entities": {"keep": True},
        "summary": "leave me",
        "importance_score": 0.9,
    }
    values.update(kwargs)
    item = FeedItem(**values)
    item.id = kwargs.get("id", 11)
    return item


def test_youtube_payload_prefers_transcript_and_cleans_title():
    outcome = map_raw_payload(YOUTUBE_PAYLOAD)
    assert outcome.ok
    mapped = outcome.content
    assert mapped.title == "Building a voice-to-print mirror with Gemini"
    assert mapped.body_text == "Hello from the transcript.\n\nThis is caption line two."
    assert mapped.original_url == "https://www.youtube.com/watch?v=8Xv5BLnXzgI"
    assert mapped.author == "Google for Developers"
    assert mapped.published_at == datetime(2026, 10, 1, 19, 0, 13)
    assert mapped.content_hash == content_hash(mapped.title, mapped.body_text)
    assert len(mapped.content_hash) == 64


def test_youtube_payload_falls_back_to_cleaned_description():
    payload = dict(YOUTUBE_PAYLOAD)
    payload["transcript"] = None
    outcome = map_raw_payload(payload)
    assert outcome.ok
    assert outcome.content.body_text == "Gayatri talks about Gemini."


def test_youtube_payload_builds_watch_url_from_video_id():
    payload = dict(YOUTUBE_PAYLOAD)
    payload["link"] = ""
    outcome = map_raw_payload(payload)
    assert outcome.ok
    assert outcome.content.original_url == "https://www.youtube.com/watch?v=8Xv5BLnXzgI"


def test_generic_payload_maps_common_keys_and_collapses_whitespace():
    outcome = map_raw_payload(
        {
            "title": "Generic article",
            "url": "https://example.com/post",
            "body": "Hello   world.\n\n\nSecond   paragraph.",
            "author": "Ada",
            "published_at": "2026-09-30T12:00:00Z",
        }
    )
    assert outcome.ok
    mapped = outcome.content
    assert mapped.title == "Generic article"
    assert mapped.body_text == "Hello world.\n\nSecond paragraph."
    assert mapped.original_url == "https://example.com/post"
    assert mapped.author == "Ada"
    assert mapped.published_at == datetime(2026, 9, 30, 12, 0, 0)


def test_nested_html_content_and_rfc2822_date():
    outcome = map_raw_payload(
        {
            "headline": "<b>Hello</b>",
            "link": " https://example.com/a ",
            "content": {"content": "<p>One</p><p>Two</p>"},
            "author_name": "  Bob  ",
            "pubDate": "Wed, 01 Oct 2026 19:00:13 +0000",
        }
    )
    assert outcome.ok
    assert outcome.content.title == "Hello"
    assert outcome.content.body_text == "One Two"
    assert outcome.content.author == "Bob"
    assert outcome.content.published_at == datetime(2026, 10, 1, 19, 0, 13)


def test_exact_duplicate_content_shares_hash():
    first = map_raw_payload({"title": "Same", "body": "Text  here", "url": "https://a.example/1"})
    second = map_raw_payload({"title": "Same", "body": "Text   here", "url": "https://b.example/2"})
    assert first.content.content_hash == second.content.content_hash
    third = map_raw_payload({"title": "Same", "body": "Other text", "url": "https://a.example/1"})
    assert third.content.content_hash != first.content.content_hash


def test_empty_title_and_author_become_none():
    outcome = map_raw_payload(
        {"title": "   ", "body": "x", "url": "https://example.com/x", "author": ""}
    )
    assert outcome.ok
    assert outcome.content.title is None
    assert outcome.content.author is None
    assert outcome.content.body_text == "x"


def test_author_is_clipped_to_255():
    outcome = map_raw_payload(
        {"title": "T", "url": "https://example.com/x", "body": "b", "author": "a" * 300}
    )
    assert outcome.ok
    assert outcome.content.author == "a" * 255


def test_invalid_published_at_still_processes():
    outcome = map_raw_payload(
        {"title": "T", "url": "https://example.com/x", "body": "b", "published": "not-a-date"}
    )
    assert outcome.ok
    assert outcome.content.published_at is None


def test_malformed_payload_fails():
    for payload in (None, [], "raw", 1):
        outcome = map_raw_payload(payload)
        assert not outcome.ok
        assert outcome.reason == "raw_payload is not an object"


def test_missing_url_fails_without_fallback():
    outcome = map_raw_payload({"title": "No link"})
    assert not outcome.ok
    assert outcome.reason == "original_url is missing"


def test_missing_url_uses_existing_feed_item_fields():
    item = _item(
        raw_payload={"title": "From payload"},
        original_url="https://example.com/kept",
        body_text="kept body",
    )
    outcome = map_raw_payload(item.raw_payload, fallback=item)
    assert outcome.ok
    assert outcome.content.original_url == "https://example.com/kept"
    assert outcome.content.body_text == "kept body"
    assert outcome.content.title == "From payload"


def test_normalize_feed_item_writes_fields_keeps_raw_and_ai_columns():
    payload = dict(YOUTUBE_PAYLOAD)
    item = _item(raw_payload=payload)
    raw_id = id(item.raw_payload)
    now = datetime(2026, 10, 6, 8, 0, 0)
    outcome = normalize_feed_item(item, now=now)
    assert outcome.ok
    assert item.processing_status is ProcessingStatus.NORMALIZED
    assert item.title == "Building a voice-to-print mirror with Gemini"
    assert item.body_text.startswith("Hello from the transcript")
    assert item.content_hash == outcome.content.content_hash
    assert item.updated_at == now
    assert item.raw_payload is payload
    assert id(item.raw_payload) == raw_id
    assert item.extracted_entities == {"keep": True}
    assert item.summary == "leave me"
    assert item.importance_score == 0.9


def test_normalize_feed_item_marks_failed_and_leaves_fields():
    item = _item(raw_payload=None, title="Old title")
    outcome = normalize_feed_item(item, now=datetime(2026, 10, 6, 8, 0, 0))
    assert not outcome.ok
    assert item.processing_status is ProcessingStatus.FAILED
    assert item.title == "Old title"
    assert item.body_text == "old body"
    assert item.content_hash is None
    assert item.raw_payload is None


def test_normalize_pending_skips_non_pending_and_counts_failures():
    ok_item = _item(id=1, external_id="ok")
    bad_item = _item(id=2, external_id="bad", raw_payload=["nope"])
    done_item = _item(id=3, external_id="done", processing_status=ProcessingStatus.PROCESSED)
    session = _FakeSession([ok_item, bad_item, done_item])
    stats = normalize_pending(session, now=datetime(2026, 10, 6, 8, 0, 0))
    assert stats.processed == 1
    assert stats.failed == 1
    assert stats.skipped == 1
    assert ok_item.processing_status is ProcessingStatus.NORMALIZED
    assert bad_item.processing_status is ProcessingStatus.FAILED
    assert done_item.processing_status is ProcessingStatus.PROCESSED


def test_list_pending_uses_session_rows():
    pending = _item()
    session = _FakeSession([pending])
    assert list_pending_feed_items(session) == [pending]


def test_clean_text_keeps_transcript_newlines():
    assert clean_text("Hello from the transcript.\n\nThis is caption line two.") == (
        "Hello from the transcript.\n\nThis is caption line two."
    )


def test_cli_fixture_example(capsys):
    assert main(["--fixture", str(FIXTURE)]) == 0
    output = capsys.readouterr().out
    assert "Building a voice-to-print mirror with Gemini" in output
    assert "https://www.youtube.com/watch?v=8Xv5BLnXzgI" in output
    assert "Generic article" in output
    assert "failed\toriginal_url is missing" in output
    assert "processed=2 failed=1" in output


def test_cli_fixture_limit(capsys):
    assert main(["--fixture", str(FIXTURE), "--limit", "1"]) == 0
    output = capsys.readouterr().out
    assert "processed=1 failed=0" in output
    assert "Generic article" not in output


def test_cli_missing_fixture_exits_2():
    assert main(["--fixture", "tests/fixtures/normalize/missing.json"]) == 2


def test_cli_rejects_limit_zero():
    assert main(["--limit", "0"]) == 2


def test_cli_rejects_fixture_with_item_id():
    assert main(["--fixture", str(FIXTURE), "--item-id", "1"]) == 2
