import asyncio
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.api import habr
from app.connectors.habr.client import HabrClient
from app.main import app
from app.models.feed_item import FeedItem
from app.schemas.habr import HabrSyncResponse
from app.services.habr import HabrService


def test_habr_preview_uses_full_article_and_keeps_health_route(monkeypatch):
    class FakeHabrClient:
        clean_html = staticmethod(HabrClient.clean_html)

        async def get_articles_page(self, page=1):
            assert page == 1
            return {
                "articleIds": ["123"],
                "articleRefs": {
                    "123": {
                        "id": "123",
                        "titleHtml": "<b>Заголовок</b>",
                        "textHtml": "Краткое описание",
                        "author": {"alias": "author"},
                        "timePublished": "2026-10-07T10:00:00+00:00",
                        "hubs": [{"title": "Python"}],
                        "tags": [{"titleHtml": "<i>тег</i>"}],
                        "statistics": {"score": 7},
                        "readingTime": 4,
                    }
                },
            }

        async def get_full_article_content(self, article_id):
            assert article_id == "123"
            return "Полный текст статьи"

    monkeypatch.setattr(habr, "HabrClient", FakeHabrClient)
    with TestClient(app) as client:
        response = client.get("/api/habr/preview?limit=1")
        assert client.get("/health").json() == {"status": "ok"}

    assert response.status_code == 200
    assert response.json() == [
        {
            "habr_id": "123",
            "title": "Заголовок",
            "content": "Полный текст статьи",
            "author": "author",
            "published_at": "2026-10-07T10:00:00Z",
            "hubs": ["Python"],
            "tags": ["тег"],
            "score": 7,
            "reading_time": 4,
            "original_url": "https://habr.com/ru/articles/123/",
        }
    ]


def test_habr_html_cleanup_handles_nested_tags():
    assert HabrClient.clean_html("<div>Первый <b>важный</b> текст</div>") == (
        "Первый важный текст"
    )


def test_habr_preview_reports_failure_when_all_articles_are_invalid(monkeypatch):
    class BrokenClient:
        clean_html = staticmethod(HabrClient.clean_html)

        async def get_articles_page(self, page=1):
            return {"articleIds": ["123"], "articleRefs": {"123": {"id": "123"}}}

        async def get_full_article_content(self, article_id):
            return "body"

    monkeypatch.setattr(habr, "HabrClient", BrokenClient)
    with TestClient(app) as client:
        response = client.get("/api/habr/preview?limit=1")
    assert response.status_code == 502


def test_habr_sync_and_saved_use_shared_feed_items(monkeypatch):
    item = FeedItem(
        source_id=1,
        external_id="123",
        title="Article",
        body_text="Full text",
        original_url="https://habr.com/ru/articles/123/",
    )
    item.id = 7

    class FakeService:
        def __init__(self, db):
            assert db is fake_db

        async def sync_publications(self, pages=1, sort="date"):
            assert pages == 1 and sort == "date"
            return HabrSyncResponse(fetched=1, saved=1, skipped=0)

        def get_posts(self, limit=20, offset=0):
            assert limit == 20 and offset == 0
            return [item]

    fake_db = object()
    monkeypatch.setattr(habr, "HabrService", FakeService)
    app.dependency_overrides[habr.get_db] = lambda: fake_db
    try:
        with TestClient(app) as client:
            sync = client.post("/api/habr/sync", json={"pages": 1})
            saved = client.get("/api/habr/saved")
    finally:
        app.dependency_overrides.pop(habr.get_db, None)

    assert sync.status_code == 200
    assert sync.json() == {"fetched": 1, "saved": 1, "skipped": 0}
    assert saved.status_code == 200
    assert saved.json()[0]["original_url"] == item.original_url


def test_habr_sync_limit_stores_three_from_a_longer_list():
    article_ids = ["101", "102", "103", "104", "105"]
    pages = []
    full_text_ids = []

    class FakeClient:
        clean_html = staticmethod(HabrClient.clean_html)

        async def get_articles_page(self, page=1, sort="date"):
            pages.append((page, sort))
            return {
                "articleIds": article_ids,
                "articleRefs": {
                    article_id: {
                        "id": article_id,
                        "titleHtml": f"Title {article_id}",
                        "textHtml": "lead",
                        "author": {"alias": "author"},
                        "timePublished": "2026-10-07T10:00:00+00:00",
                    }
                    for article_id in article_ids
                },
            }

        async def get_full_article_content(self, article_id):
            full_text_ids.append(article_id)
            return f"full {article_id}"

    class FakeDb:
        def __init__(self):
            self.added = []
            self.committed = False

        def scalar(self, _statement):
            return 1

        def scalars(self, _statement):
            return SimpleNamespace(all=lambda: [])

        def add(self, item):
            self.added.append(item)

        def commit(self):
            self.committed = True

    db = FakeDb()
    stats = asyncio.run(HabrService(db, FakeClient()).sync_publications(pages=2, limit=3))

    assert stats.fetched == 3
    assert stats.saved == 3
    assert [item.external_id for item in db.added] == article_ids[:3]
    assert full_text_ids == article_ids[:3]
    assert pages == [(1, "date")]
    assert db.committed is True


def test_habr_sync_commits_source_and_each_page_before_later_failure():
    """Сеть не идёт внутри транзакции, а сбой страницы не теряет предыдущие."""
    events = []

    class FakeClient:
        clean_html = staticmethod(HabrClient.clean_html)

        async def get_articles_page(self, page=1, sort="date"):
            events.append(f"page{page}_network")
            if page == 2:
                raise RuntimeError("habr down")
            return {
                "articleIds": ["101"],
                "articleRefs": {
                    "101": {
                        "id": "101",
                        "titleHtml": "Title",
                        "textHtml": "lead",
                        "author": {"alias": "author"},
                        "timePublished": "2026-10-07T10:00:00+00:00",
                    }
                },
            }

        async def get_full_article_content(self, article_id):
            events.append(f"full:{article_id}")
            return f"full {article_id}"

    class FakeDb:
        def __init__(self):
            self.commits = 0
            self.added = []

        def scalar(self, _statement):
            events.append("source_upsert")
            return 1

        def scalars(self, _statement):
            events.append("existing_read")
            return SimpleNamespace(all=lambda: [])

        def add(self, item):
            self.added.append(item)
            events.append(f"add:{item.external_id}")

        def commit(self):
            self.commits += 1
            events.append("commit")

    db = FakeDb()
    with pytest.raises(RuntimeError, match="habr down"):
        asyncio.run(HabrService(db, FakeClient()).sync_publications(pages=2))

    # Источник и прочитанные id фиксируются до сетевых вызовов.
    assert events.index("source_upsert") < events.index("page1_network")
    assert events.index("commit") < events.index("page1_network")
    first_read_commit = events.index("commit", events.index("existing_read"))
    assert first_read_commit < events.index("full:101")
    # Первая страница сохранена и закоммичена до падения второй.
    assert [item.external_id for item in db.added] == ["101"]
    saved_commit = events.index("commit", events.index("add:101"))
    assert saved_commit < events.index("page2_network")
    assert db.commits == 3
