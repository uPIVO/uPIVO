"""Store Habr articles in the shared source/feed item tables."""

from datetime import UTC

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.connectors.habr.client import HabrClient
from app.models.enums import SourceType
from app.models.feed_item import FeedItem
from app.models.source import Source
from app.schemas.habr import HabrArticlePayload, HabrSyncResponse


class HabrService:
    def __init__(self, db: Session, client: HabrClient | None = None):
        self.db = db
        self.client = client or HabrClient()

    async def sync_publications(self, pages: int = 1, sort: str = "date") -> HabrSyncResponse:
        source = self.db.scalar(
            select(Source).where(
                Source.type == SourceType.HABR,
                Source.canonical_url == HabrClient.RSS_URL,
            )
        )
        if source is None:
            source = Source(
                type=SourceType.HABR,
                name="Habr",
                canonical_url=HabrClient.RSS_URL,
                external_id="habr:articles",
            )
            self.db.add(source)
            self.db.flush()

        fetched = saved = skipped = 0
        seen: set[str] = set()
        for page in range(1, pages + 1):
            data = await self.client.get_articles_page(page=page, sort=sort)
            article_ids = [str(article_id) for article_id in data.get("articleIds", [])]
            if not article_ids:
                break
            fetched += len(article_ids)
            existing = set(
                self.db.scalars(
                    select(FeedItem.external_id).where(
                        FeedItem.source_id == source.id,
                        FeedItem.external_id.in_(article_ids),
                    )
                ).all()
            )

            for article_id in article_ids:
                if article_id in existing or article_id in seen:
                    skipped += 1
                    continue
                raw = data.get("articleRefs", {}).get(article_id)
                if not isinstance(raw, dict):
                    continue
                article = HabrArticlePayload.model_validate(raw)
                content = await self.client.get_full_article_content(article_id)
                if not content:
                    content = self.client.clean_html(
                        article.text_html or (article.lead_data or {}).get("textHtml", "")
                    )

                url = f"https://habr.com/ru/articles/{article_id}/"
                title = self.client.clean_html(article.title_html)
                author = article.author.alias if article.author else "Unknown"
                published = article.time_published.astimezone(UTC).replace(tzinfo=None)
                self.db.add(
                    FeedItem(
                        source_id=source.id,
                        external_id=article_id,
                        title=title,
                        body_text=content,
                        original_url=url,
                        author=author,
                        published_at=published,
                        raw_payload={
                            "kind": "habr_article",
                            "title": title,
                            "body_text": content,
                            "original_url": url,
                            "author": author,
                            "published_at": published.isoformat(),
                            "hubs": [hub.title for hub in article.hubs],
                            "tags": [tag.title_html for tag in article.tags],
                        },
                    )
                )
                seen.add(article_id)
                saved += 1

        self.db.commit()
        return HabrSyncResponse(fetched=fetched, saved=saved, skipped=skipped)

    def get_posts(self, limit: int = 20, offset: int = 0) -> list[FeedItem]:
        statement = (
            select(FeedItem)
            .join(Source, FeedItem.source_id == Source.id)
            .where(Source.type == SourceType.HABR)
            .order_by(FeedItem.published_at.desc())
            .offset(offset)
            .limit(limit)
        )
        return list(self.db.scalars(statement).all())
