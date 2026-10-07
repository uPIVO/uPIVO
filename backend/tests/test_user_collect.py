from datetime import datetime

import pytest

from app.connectors.base import FetchResult, RawPublication, SourceFetchError
from app.models.enums import ProcessingStatus, SourceType
from app.models.feed_item import FeedItem
from app.models.source import Source
from app.models.user import User
from app.models.user_source import UserSource
from app.services.__main__ import main
from app.services.collect import UnknownUserError, collect_for_user, resolve_user

CHANNEL_ID = "UC_x5XG1OV2P6uZZ5FSM9Ttw"
CHANNEL_URL = f"https://www.youtube.com/channel/{CHANNEL_ID}"
YOUTUBE_PAYLOAD = {
    "kind": "youtube_rss_entry",
    "video_id": "8Xv5BLnXzgI",
    "channel_id": CHANNEL_ID,
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


class FakeSession:
    def __init__(self, user=None, links=None, source=None, items=None):
        self.user = user
        self.links = list(links or [])
        self.source = source
        self.items = list(items or [])
        self.added = []

    def scalar(self, stmt):
        entity = stmt.column_descriptions[0]["entity"]
        if entity is User:
            return self.user
        if entity is Source:
            return self.source
        return None

    def execute(self, stmt):
        return _Rows(self.links)

    def scalars(self, stmt):
        return _Rows(self.items)

    def add(self, obj):
        self.added.append(obj)
        if isinstance(obj, Source):
            obj.id = 7
            self.source = obj
        elif isinstance(obj, FeedItem):
            self.items.append(obj)

    def flush(self):
        if self.source is not None and self.source.id is None:
            self.source.id = 7


class ScriptedYouTube:
    def __init__(self, by_url: dict[str, FetchResult | BaseException]):
        self.by_url = by_url
        self.calls: list[tuple[str, int | None]] = []

    def fetch(self, canonical_url: str, *, limit: int | None = None) -> FetchResult:
        self.calls.append((canonical_url, limit))
        result = self.by_url[canonical_url]
        if isinstance(result, BaseException):
            raise result
        return result


class DummySession:
    def __init__(self, engine=None):
        self.committed = False
        self.rolled_back = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True


def _user(*, user_id=1, email="demo@example.com") -> User:
    user = User(email=email, password_hash="local-dev-not-auth")
    user.id = user_id
    return user


def _source(
    *,
    source_id=7,
    source_type=SourceType.YOUTUBE,
    url=CHANNEL_URL,
    external_id=CHANNEL_ID,
    name="Google for Developers",
) -> Source:
    source = Source(
        type=source_type,
        name=name,
        canonical_url=url,
        external_id=external_id,
    )
    source.id = source_id
    return source


def _link(*, link_id=3, user_id=1, source_id=7, enabled=True) -> UserSource:
    link = UserSource(user_id=user_id, source_id=source_id, enabled=enabled)
    link.id = link_id
    return link


def _video_fetch() -> FetchResult:
    return FetchResult(
        source_name="Google for Developers",
        source_external_id=CHANNEL_ID,
        canonical_url=CHANNEL_URL,
        items=[
            RawPublication(
                external_id="8Xv5BLnXzgI",
                original_url="https://www.youtube.com/watch?v=8Xv5BLnXzgI",
                title="Building a voice-to-print mirror with Gemini",
                body_text="description",
                published_at=datetime(2026, 10, 1, 19, 0, 13),
                author="Google for Developers",
                raw_payload=dict(YOUTUBE_PAYLOAD),
            )
        ],
    )


def _empty_fetch() -> FetchResult:
    return FetchResult(
        source_name="Google for Developers",
        source_external_id=CHANNEL_ID,
        canonical_url=CHANNEL_URL,
        items=[],
    )


def _cli(monkeypatch, collect):
    monkeypatch.setattr("app.services.__main__.get_engine", lambda: object())
    monkeypatch.setattr("app.services.__main__.Session", DummySession)
    monkeypatch.setattr("app.services.__main__.collect_for_user", collect)


def test_resolve_user_by_id_and_email():
    user = _user()
    session = FakeSession(user=user)
    assert resolve_user(session, "1") is user
    assert resolve_user(session, "demo@example.com") is user
    assert resolve_user(session, "  demo@example.com  ") is user


def test_resolve_user_unknown_and_blank():
    session = FakeSession(user=None)
    assert resolve_user(session, "99") is None
    assert resolve_user(session, "nobody@example.com") is None
    assert resolve_user(session, "   ") is None


def test_unknown_user_raises():
    session = FakeSession(user=None)
    with pytest.raises(UnknownUserError):
        collect_for_user(session, "nobody@example.com", youtube=ScriptedYouTube({}))


def test_user_with_no_sources_collects_nothing():
    user = _user()
    yt = ScriptedYouTube({})
    result = collect_for_user(FakeSession(user=user), "demo@example.com", youtube=yt)
    assert result.user_id == 1
    assert result.email == "demo@example.com"
    assert result.outcomes == []
    assert result.collected == 0
    assert yt.calls == []


def test_disabled_source_is_skipped():
    user = _user()
    source = _source()
    link = _link(enabled=False)
    yt = ScriptedYouTube({CHANNEL_URL: _video_fetch()})
    session = FakeSession(user=user, links=[(link, source)], source=source)
    result = collect_for_user(session, "1", youtube=yt)
    assert len(result.outcomes) == 1
    assert result.outcomes[0].status == "skipped"
    assert result.outcomes[0].reason == "disabled"
    assert result.skipped == 1
    assert yt.calls == []
    assert session.added == []


def test_unsupported_source_type_is_skipped():
    user = _user()
    source = _source(
        source_type=SourceType.RSS,
        url="https://example.com/feed.xml",
        external_id="https://example.com/feed.xml",
        name="Example RSS",
    )
    link = _link()
    yt = ScriptedYouTube({})
    session = FakeSession(user=user, links=[(link, source)], source=source)
    result = collect_for_user(session, "demo@example.com", youtube=yt)
    assert result.outcomes[0].status == "skipped"
    assert result.outcomes[0].reason == "unsupported:rss"
    assert yt.calls == []


def test_empty_fetch_marks_source_empty_and_reuses_source_row():
    user = _user()
    source = _source()
    link = _link()
    yt = ScriptedYouTube({CHANNEL_URL: _empty_fetch()})
    session = FakeSession(user=user, links=[(link, source)], source=source)
    result = collect_for_user(session, "demo@example.com", youtube=yt)
    assert result.outcomes[0].status == "empty"
    assert result.outcomes[0].fetched == 0
    assert result.outcomes[0].inserted == 0
    assert result.empty == 1
    assert yt.calls == [(CHANNEL_URL, 15)]
    assert not any(isinstance(obj, Source) for obj in session.added)
    assert source.last_synced_at is not None


def test_collect_reuses_user_source_row_when_lookup_would_miss():
    user = _user()
    source = _source(source_id=5, external_id=None)
    link = _link(source_id=5)
    yt = ScriptedYouTube({CHANNEL_URL: _video_fetch()})
    session = FakeSession(user=user, links=[(link, source)], source=None)
    result = collect_for_user(session, "demo@example.com", youtube=yt)
    assert result.outcomes[0].source_id == 5
    assert source.external_id == CHANNEL_ID
    assert not any(isinstance(obj, Source) for obj in session.added)
    assert session.items[0].source_id == 5


def test_youtube_collect_writes_shared_feed_items_once():
    user = _user()
    source = _source()
    link = _link()
    yt = ScriptedYouTube({CHANNEL_URL: _video_fetch()})
    session = FakeSession(user=user, links=[(link, source)], source=source)
    result = collect_for_user(session, "1", youtube=yt)
    assert result.outcomes[0].status == "collected"
    assert result.outcomes[0].fetched == 1
    assert result.outcomes[0].inserted == 1
    assert result.outcomes[0].source_id == 7
    assert len(yt.calls) == 1
    assert not any(isinstance(obj, Source) for obj in session.added)
    item = session.items[0]
    assert item.source_id == 7
    assert item.external_id == "8Xv5BLnXzgI"
    assert item.processing_status is ProcessingStatus.PENDING
    assert item.raw_payload["kind"] == "youtube_rss_entry"


def test_normalize_hook_maps_collected_payload():
    user = _user()
    source = _source()
    link = _link()
    yt = ScriptedYouTube({CHANNEL_URL: _video_fetch()})
    session = FakeSession(user=user, links=[(link, source)], source=source)
    result = collect_for_user(session, "demo@example.com", youtube=yt, normalize=True)
    assert result.outcomes[0].normalized == 1
    item = session.items[0]
    assert item.processing_status is ProcessingStatus.PROCESSED
    assert item.body_text == "Hello from the transcript.\n\nThis is caption line two."
    assert item.content_hash is not None
    assert len(item.content_hash) == 64


def test_dry_run_fetches_without_persist():
    user = _user()
    source = _source()
    link = _link()
    yt = ScriptedYouTube({CHANNEL_URL: _video_fetch()})
    session = FakeSession(user=user, links=[(link, source)], source=source)
    result = collect_for_user(session, "demo@example.com", youtube=yt, dry_run=True)
    assert result.dry_run is True
    assert result.outcomes[0].status == "collected"
    assert result.outcomes[0].fetched == 1
    assert result.outcomes[0].inserted == 0
    assert session.added == []
    assert session.items == []


def test_youtube_fetch_error_is_failed_not_raised():
    user = _user()
    source = _source()
    link = _link()
    yt = ScriptedYouTube({CHANNEL_URL: SourceFetchError("Network error while reading YouTube")})
    session = FakeSession(user=user, links=[(link, source)], source=source)
    result = collect_for_user(session, "demo@example.com", youtube=yt)
    assert result.failed == 1
    assert result.outcomes[0].status == "failed"
    assert "Network error" in (result.outcomes[0].reason or "")


def test_enabled_youtube_collected_disabled_skipped():
    user = _user()
    active = _source()
    quiet = _source(
        source_id=8,
        url="https://www.youtube.com/channel/UCother",
        external_id="UCother",
    )
    yt = ScriptedYouTube({CHANNEL_URL: _video_fetch()})
    session = FakeSession(
        user=user,
        links=[(_link(enabled=False, source_id=8, link_id=1), quiet), (_link(link_id=2), active)],
        source=active,
    )
    result = collect_for_user(session, "demo@example.com", youtube=yt)
    assert [row.status for row in result.outcomes] == ["skipped", "collected"]
    assert yt.calls == [(CHANNEL_URL, 15)]


def test_cli_unknown_user_exits_2(monkeypatch, capsys):
    def boom(*args, **kwargs):
        raise UnknownUserError("nobody@example.com")

    _cli(monkeypatch, boom)
    assert main(["--user", "nobody@example.com"]) == 2
    assert "Unknown user: nobody@example.com" in capsys.readouterr().err


def test_cli_no_sources_exits_0(monkeypatch, capsys):
    def fake_collect(*args, **kwargs):
        from app.services.collect import UserCollectResult

        return UserCollectResult(user_id=1, email="demo@example.com")

    _cli(monkeypatch, fake_collect)
    assert main(["--user", "demo@example.com"]) == 0
    output = capsys.readouterr().out
    assert "user_id=1 email=demo@example.com" in output
    assert "sources=0" in output
    assert "collected=0 empty=0 skipped=0 failed=0" in output


def test_cli_dry_run_rollbacks(monkeypatch, capsys):
    from app.services.collect import SourceCollectOutcome, UserCollectResult

    def fake_collect(*args, **kwargs):
        assert kwargs["dry_run"] is True
        return UserCollectResult(
            user_id=1,
            email="demo@example.com",
            dry_run=True,
            outcomes=[
                SourceCollectOutcome(
                    source_id=7,
                    source_type="youtube",
                    canonical_url=CHANNEL_URL,
                    status="collected",
                    fetched=2,
                )
            ],
        )

    session = DummySession()
    monkeypatch.setattr("app.services.__main__.get_engine", lambda: object())
    monkeypatch.setattr("app.services.__main__.Session", lambda engine: session)
    monkeypatch.setattr("app.services.__main__.collect_for_user", fake_collect)
    assert main(["--user", "1", "--dry-run"]) == 0
    assert session.rolled_back is True
    assert session.committed is False
    assert "dry-run" in capsys.readouterr().out


def test_cli_failed_source_exits_1(monkeypatch, capsys):
    from app.services.collect import SourceCollectOutcome, UserCollectResult

    def fake_collect(*args, **kwargs):
        return UserCollectResult(
            user_id=1,
            email="demo@example.com",
            outcomes=[
                SourceCollectOutcome(
                    source_id=7,
                    source_type="youtube",
                    canonical_url=CHANNEL_URL,
                    status="failed",
                    reason="Network error while reading YouTube",
                )
            ],
        )

    _cli(monkeypatch, fake_collect)
    assert main(["--user", "demo@example.com"]) == 1
    output = capsys.readouterr().out
    assert "status=failed" in output
    assert "failed=1" in output


def test_cli_rejects_limit_zero():
    assert main(["--user", "1", "--limit", "0"]) == 2


def test_cli_requires_user():
    with pytest.raises(SystemExit) as exc:
        main([])
    assert exc.value.code == 2
