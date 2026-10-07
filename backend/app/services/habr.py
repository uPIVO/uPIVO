from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.connectors.habr.client import HabrClient
from app.models.habr import HabrPost
from app.schemas.habr import HabrArticlePayload, HabrPostCreate, HabrSyncResponse


class HabrService:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.client = HabrClient()

    async def sync_publications(self, pages: int = 1, sort: str = "date") -> HabrSyncResponse:
        total_fetched = 0
        saved_count = 0
        skipped_count = 0

        for page in range(1, pages + 1):
            data = await self.client.get_articles_page(page=page, sort=sort)
            article_ids = data.get("articleIds", [])
            article_refs = data.get("articleRefs", {})

            if not article_ids:
                break

            total_fetched += len(article_ids)

            # Проверяем, какие посты уже есть в базе
            existing_query = select(HabrPost.habr_id).where(HabrPost.habr_id.in_(article_ids))
            existing_result = await self.db.execute(existing_query)
            existing_ids = set(existing_result.scalars().all())

            for art_id in article_ids:
                if str(art_id) in existing_ids:
                    skipped_count += 1
                    continue

                raw_data = article_refs.get(str(art_id))
                if not raw_data:
                    continue

                parsed_article = HabrArticlePayload.model_validate(raw_data)

                # Текст поста: если в превью его нет, запрашиваем детальную статью
                content = parsed_article.text_html
                if not content and parsed_article.lead_data:
                    content = parsed_article.lead_data.get("textHtml", "")

                if not content:
                    try:
                        detail = await self.client.get_article_details(parsed_article.id)
                        content = detail.get("textHtml", "")
                    except Exception:
                        content = ""

                author_name = (
                    parsed_article.author.alias
                    if parsed_article.author
                    else "Unknown"
                )
                score = parsed_article.statistics.score if parsed_article.statistics else 0
                hubs = [h.title for h in parsed_article.hubs]
                tags = [self.client.clean_html(t.title_html) for t in parsed_article.tags]
                title = self.client.clean_html(parsed_article.title_html)
                clean_content = self.client.clean_html(content)
                original_url = f"https://habr.com/ru/articles/{parsed_article.id}/"

                db_post = HabrPost(
                    habr_id=parsed_article.id,
                    title=title,
                    content=clean_content,
                    author=author_name,
                    published_at=parsed_article.time_published,
                    hubs=hubs,
                    tags=tags,
                    score=score,
                    reading_time=parsed_article.reading_time,
                    original_url=original_url,
                )
                self.db.add(db_post)
                saved_count += 1

            await self.db.commit()

        return HabrSyncResponse(
            fetched=total_fetched,
            saved=saved_count,
            skipped=skipped_count,
        )

    async def get_posts(self, limit: int = 20, offset: int = 0) -> list[HabrPost]:
        query = (
            select(HabrPost)
            .order_by(HabrPost.published_at.desc())
            .offset(offset)
            .limit(limit)
        )
        result = await self.db.execute(query)
        return list(result.scalars().all())