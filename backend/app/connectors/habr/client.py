from datetime import datetime
from email.utils import parsedate_to_datetime
import html
import logging
import re
from typing import Any
import xml.etree.ElementTree as ET
import httpx

logger = logging.getLogger(__name__)


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
        if text.endswith("Читать далее"):
            text = text[:-12].strip()
        return text

    async def get_articles_page(
        self,
        page: int = 1,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Получение списка статей: API -> при ошибке fallback на RSS."""
        url = f"{self.BASE_URL}/articles"
        params = {"page": page}

        logger.info(f"[HabrClient] Запрос списка статей: {url} (page={page})")

        # 1. Попытка запроса к API
        try:
            async with httpx.AsyncClient(headers=self.headers, timeout=self.timeout) as client:
                response = await client.get(url, params=params)
                if response.status_code == 200:
                    data = response.json()
                    logger.info(f"[HabrClient] Успешно получено {len(data.get('articleIds', []))} статей через API")
                    return data
                
                logger.warning(
                    f"[HabrClient] API Хабра вернул код {response.status_code}: {response.text[:200]}. "
                    f"Переключаемся на резервный RSS-поток."
                )
        except Exception as e:
            logger.warning(f"[HabrClient] Сбой при запросе к API Хабра ({e}). Переключаемся на RSS.")

        # 2. Резервный источник: RSS
        return await self._fetch_from_rss()

    async def _fetch_from_rss(self) -> dict[str, Any]:
        """Парсинг официального RSS-канала Хабра."""
        logger.info(f"[HabrClient] Запрос статей через RSS: {self.RSS_URL}")
        try:
            async with httpx.AsyncClient(headers=self.headers, timeout=self.timeout) as client:
                resp = await client.get(self.RSS_URL)
                resp.raise_for_status()
                rss_text = resp.text

            root = ET.fromstring(rss_text)
            channel = root.find("channel")
            if channel is None:
                logger.error("[HabrClient] В RSS-ответе не найден тег <channel>")
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
                except Exception as date_err:
                    logger.debug(f"[HabrClient] Ошибка парсинга даты '{pub_date_str}' для статьи {art_id}: {date_err}")
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

            logger.info(f"[HabrClient] Успешно получено {len(article_ids)} статей через RSS")
            return {
                "articleIds": article_ids,
                "articleRefs": article_refs,
            }
        except Exception as e:
            logger.error(f"[HabrClient] Критическая ошибка при парсинге RSS: {e}", exc_info=True)
            return {"articleIds": [], "articleRefs": {}}

    async def get_full_article_content(self, article_id: str) -> str:
        """Загрузка полного текста статьи по ID."""
        # 1. Попытка через API
        api_url = f"{self.BASE_URL}/articles/{article_id}"
        try:
            async with httpx.AsyncClient(headers=self.headers, timeout=self.timeout) as client:
                res = await client.get(api_url)
                if res.status_code == 200:
                    text_html = res.json().get("textHtml")
                    if text_html:
                        return self.clean_html(text_html)
                else:
                    logger.debug(f"[HabrClient] API статьи {article_id} вернул {res.status_code}")
        except Exception as e:
            logger.debug(f"[HabrClient] Ошибка получения статьи {article_id} через API: {e}")

        # 2. Попытка парсинга веб-страницы
        page_url = f"https://habr.com/ru/articles/{article_id}/"
        try:
            async with httpx.AsyncClient(headers=self.headers, timeout=self.timeout, follow_redirects=True) as client:
                res = await client.get(page_url)
                if res.status_code == 200:
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
                    
                    logger.warning(f"[HabrClient] На странице {page_url} не найден контейнер текста статьи")
                else:
                    logger.warning(f"[HabrClient] Не удалось открыть страницу {page_url} (HTTP {res.status_code})")
        except Exception as e:
            logger.warning(f"[HabrClient] Ошибка при загрузке страницы {page_url}: {e}")

        return ""