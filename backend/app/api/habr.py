import asyncio
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.connectors.habr.client import HabrClient
from app.db.session import get_db
from app.schemas.habr import FeedItemResponse, HabrPostBase, HabrSyncRequest, HabrSyncResponse
from app.services.habr import HabrService

router = APIRouter(prefix="/habr", tags=["Habr Parser"])
logger = logging.getLogger(__name__)


@router.get("/preview", response_model=list[HabrPostBase], summary="Парсинг полных статей с Хабра")
async def preview_habr_posts(
    limit: int = Query(default=3, ge=1, le=10, description="Количество постов"),
):
    client = HabrClient()
    try:
        data = await client.get_articles_page(page=1)
    except Exception as exc:
        logger.exception("Habr article list fetch failed")
        raise HTTPException(status_code=502, detail="Не удалось получить статьи Хабра") from exc

    article_ids = data.get("articleIds", [])[:limit]
    article_refs = data.get("articleRefs", {})

    if not article_ids:
        return []

    # 1. Параллельно загружаем полные тексты для всех запрошенных статей
    full_texts = await asyncio.gather(
        *(client.get_full_article_content(str(art_id)) for art_id in article_ids),
        return_exceptions=True,
    )

    results: list[HabrPostBase] = []
    for art_id, full_content in zip(article_ids, full_texts, strict=True):
        raw = article_refs.get(str(art_id))
        if not raw:
            logger.warning("Habr article %s is missing from articleRefs", art_id)
            continue

        try:
            author_data = raw.get("author") or {}
            stats_data = raw.get("statistics") or {}

            # Если полный текст загрузился — берем его, иначе превью
            if isinstance(full_content, str) and full_content:
                final_content = full_content
            else:
                if isinstance(full_content, Exception):
                    logger.warning(
                        "Habr full text failed for article %s", art_id, exc_info=full_content
                    )
                final_content = client.clean_html(raw.get("textHtml") or "")

            results.append(
                HabrPostBase(
                    habr_id=str(raw.get("id")),
                    title=client.clean_html(raw.get("titleHtml", "Без названия")),
                    content=final_content,
                    author=author_data.get("alias") or author_data.get("fullname") or "Unknown",
                    published_at=raw.get("timePublished"),
                    hubs=[h.get("title", "") for h in raw.get("hubs", []) if isinstance(h, dict)],
                    tags=[
                        client.clean_html(t.get("titleHtml", ""))
                        for t in raw.get("tags", [])
                        if isinstance(t, dict)
                    ],
                    score=stats_data.get("score", 0),
                    reading_time=raw.get("readingTime", 0),
                    original_url=f"https://habr.com/ru/articles/{raw.get('id')}/",
                )
            )
        except Exception:
            logger.exception("Habr article %s could not be parsed", art_id)
            continue

    if not results:
        raise HTTPException(status_code=502, detail="Не удалось обработать статьи Хабра")
    return results


@router.post("/sync", response_model=HabrSyncResponse, summary="Сохранить статьи Хабра")
async def sync_habr_posts(
    payload: HabrSyncRequest,
    db: Annotated[Session, Depends(get_db)],
):
    try:
        return await HabrService(db).sync_publications(pages=payload.pages, sort=payload.sort)
    except Exception as exc:
        db.rollback()
        logger.exception("Habr article sync failed")
        raise HTTPException(status_code=502, detail="Не удалось сохранить статьи Хабра") from exc


@router.get("/saved", response_model=list[FeedItemResponse], summary="Сохранённые статьи Хабра")
def list_saved_habr_posts(
    db: Annotated[Session, Depends(get_db)],
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
):
    return HabrService(db).get_posts(limit=limit, offset=offset)
