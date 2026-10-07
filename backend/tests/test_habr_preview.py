from fastapi.testclient import TestClient

from app.api import habr
from app.connectors.habr.client import HabrClient
from app.main import app


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
