import asyncio
import logging
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.feed_item import FeedItem
from app.connectors.habr.client import HabrClient
from app.schemas.habr import HabrSyncResponse

logger = logging.getLogger(__name__)


class HabrService:
    SOURCE_NAME = "habr"

    def __init__(self, db: AsyncSession):
        self.db = db
        self.client = HabrClient()

    async def sync_publications(self, pages: int = 1) -> HabrSyncResponse:
        total_fetched = 0
        saved_count = 0
        skipped_count = 0

        for page in range(1, pages + 1):
            logger.info(f"[HabrService] Локальный сбор страницы {page}...")
            try:
                data = await self.client.get_articles_page(page=page)
            except Exception as e:
                logger.error(f"[HabrService] Сбой при получении страницы {page}: {e}")
                break

            article_ids = [str(aid) for aid in data.get("articleIds", [])]
            article_refs = data.get("articleRefs", {})

            if not article_ids:
                break

            total_fetched += len(article_ids)

            # Проверяем дубликаты в локальной БД
            existing_query = select(FeedItem.external_id).where(
                FeedItem.source == self.SOURCE_NAME,
                FeedItem.external_id.in_(article_ids),
            )
            existing_result = await self.db.execute(existing_query)
            existing_ids = set(existing_result.scalars().all())

            new_article_ids = [aid for aid in article_ids if aid not in existing_ids]
            skipped_count += len(article_ids) - len(new_article_ids)

            if not new_article_ids:
                logger.info(f"[HabrService] Все {len(article_ids)} статей уже есть в локальной базе")
                continue

            # Параллельно скачиваем полный текст новых статей
            full_texts = await asyncio.gather(
                *(self.client.get_full_article_content(aid) for aid in new_article_ids),
                return_exceptions=True,
            )

            for art_id, full_content in zip(new_article_ids, full_texts):
                raw = article_refs.get(art_id)
                if not raw:
                    continue

                try:
                    author_data = raw.get("author") or {}
                    stats_data = raw.get("statistics") or {}

                    if isinstance(full_content, str) and full_content:
                        content = full_content
                    else:
                        content = self.client.clean_html(raw.get("textHtml") or "")

                    title = self.client.clean_html(raw.get("titleHtml", "Без названия"))
                    author = author_data.get("alias") or author_data.get("fullname") or "Unknown"
                    hubs = [h.get("title", "") for h in raw.get("hubs", []) if isinstance(h, dict)]
                    tags = [self.client.clean_html(t.get("titleHtml", "")) for t in raw.get("tags", []) if isinstance(t, dict)]
                    score = stats_data.get("score", 0)
                    reading_time = raw.get("readingTime", 0)
                    original_url = f"https://habr.com/ru/articles/{art_id}/"

                    feed_item = FeedItem(
                        source=self.SOURCE_NAME,
                        external_id=art_id,
                        title=title,
                        content=content,
                        author=author,
                        url=original_url,
                        published_at=raw.get("timePublished"),
                        raw_data={
                            "hubs": hubs,
                            "tags": tags,
                            "score": score,
                            "reading_time": reading_time,
                        },
                    )

                    self.db.add(feed_item)
                    saved_count += 1
                except Exception as err:
                    logger.error(f"[HabrService] Ошибка записи статьи id={art_id}: {err}")
                    continue

            await self.db.commit()

        logger.info(f"[HabrService] Завершено. Собрано: {total_fetched}, Сохранено: {saved_count}, Пропущено: {skipped_count}")
        return HabrSyncResponse(
            fetched=total_fetched,
            saved=saved_count,
            skipped=skipped_count,
        )

    async def get_saved_posts(self, limit: int = 20, offset: int = 0) -> list[FeedItem]:
        query = (
            select(FeedItem)
            .where(FeedItem.source == self.SOURCE_NAME)
            .order_by(FeedItem.published_at.desc())
            .offset(offset)
            .limit(limit)
        )
        result = await self.db.execute(query)
        return list(result.scalars().all())