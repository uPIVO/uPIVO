from datetime import datetime
from email.utils import parsedate_to_datetime
import html
import re
from typing import Any
import xml.etree.ElementTree as ET
import httpx


class HabrClient:
    BASE_URL = "https://habr.com/kek/v2"
    RSS_URL = "https://habr.com/ru/rss/articles/"

    def __init__(self, timeout: float = 15.0):
        self.headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "*/*",
            "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
        }
        self.timeout = timeout

    @staticmethod
    def clean_html(raw_html: str | None) -> str:
        if not raw_html:
            return ""
        text = html.unescape(raw_html)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        # Убираем хвосты "Читать далее"
        if text.endswith("Читать далее"):
            text = text[:-12].strip()
        return text

    async def get_articles_page(
        self,
        page: int = 1,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Получение списка свежих статей."""
        url = f"{self.BASE_URL}/articles"
        params = {"page": page}

        try:
            async with httpx.AsyncClient(headers=self.headers, timeout=self.timeout) as client:
                response = await client.get(url, params=params)
                if response.status_code == 200:
                    return response.json()
        except Exception:
            pass

        return await self._fetch_from_rss()

    async def _fetch_from_rss(self) -> dict[str, Any]:
        """Резервный сбор через официальный RSS-канал."""
        async with httpx.AsyncClient(headers=self.headers, timeout=self.timeout) as client:
            resp = await client.get(self.RSS_URL)
            resp.raise_for_status()
            rss_text = resp.text

        root = ET.fromstring(rss_text)
        channel = root.find("channel")
        if channel is None:
            return {"articleIds": [], "articleRefs": {}}

        article_ids = []
        article_refs = {}

        for item in channel.findall("item"):
            link = item.findtext("link", "").strip()
            id_match = re.search(r"/articles/(\d+)/", link) or re.search(r"/post/(\d+)/", link)
            art_id = id_match.group(1) if id_match else str(abs(hash(link)) % 1000000)

            title = item.findtext("title", "Без названия").strip()
            description = item.findtext("description", "").strip()

            creator = (
                item.findtext("{http://purl.org/dc/elements/1.1/}creator")
                or item.findtext("author")
                or "Unknown"
            ).strip()

            pub_date_str = item.findtext("pubDate", "")
            try:
                dt = parsedate_to_datetime(pub_date_str)
                iso_date = dt.isoformat()
            except Exception:
                iso_date = datetime.utcnow().isoformat()

            categories = [c.text.strip() for c in item.findall("category") if c.text]

            article_ids.append(art_id)
            article_refs[art_id] = {
                "id": art_id,
                "titleHtml": title,
                "textHtml": description,
                "author": {"alias": creator},
                "timePublished": iso_date,
                "hubs": [{"title": c} for c in categories[:3]],
                "tags": [{"titleHtml": c} for c in categories[3:]] if len(categories) > 3 else [],
                "statistics": {"score": 0},
                "readingTime": 5,
            }

        return {
            "articleIds": article_ids,
            "articleRefs": article_refs,
        }

    async def get_full_article_content(self, article_id: str) -> str:
        """
        Загрузка полного текста статьи со страницы публикации.
        """
        # Сначала пробуем быстро через API
        url = f"{self.BASE_URL}/articles/{article_id}"
        try:
            async with httpx.AsyncClient(headers=self.headers, timeout=self.timeout) as client:
                res = await client.get(url)
                if res.status_code == 200:
                    text_html = res.json().get("textHtml")
                    if text_html:
                        return self.clean_html(text_html)
        except Exception:
            pass

        # Если через API не отдало, загружаем веб-страницу статьи
        page_url = f"https://habr.com/ru/articles/{article_id}/"
        try:
            async with httpx.AsyncClient(headers=self.headers, timeout=self.timeout, follow_redirects=True) as client:
                res = await client.get(page_url)
                if res.status_code == 200:
                    # Ищем основной контейнер статьи
                    match = re.search(
                        r'<div[^>]+id="post-content-body"[^>]*>(.*?)</div>\s*</div>',
                        res.text,
                        re.DOTALL,
                    ) or re.search(
                        r'<div[^>]+class="[^"]*article-formatted-body[^"]*"[^>]*>(.*?)</div>',
                        res.text,
                        re.DOTALL,
                    )
                    if match:
                        return self.clean_html(match.group(1))
        except Exception:
            pass

        return ""