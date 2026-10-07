# app/api/endpoints/habr.py
from fastapi import APIRouter, HTTPException, Query
from app.connectors.habr.client import HabrClient
from app.schemas.habr import HabrPostBase

router = APIRouter(prefix="/habr", tags=["Habr Parser"])


@router.get("/preview", response_model=list[HabrPostBase], summary="Тестовый парсинг без сохранения в БД")
async def preview_habr_posts(
    limit: int = Query(default=5, ge=1, le=20),
    sort: str = Query(default="date"),
):
    client = HabrClient()
    try:
        data = await client.get_articles_page(page=1, sort=sort)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Ошибка запроса к Хабру: {e}")

    article_ids = data.get("articleIds", [])[:limit]
    article_refs = data.get("articleRefs", {})

    results: list[HabrPostBase] = []
    for art_id in article_ids:
        raw = article_refs.get(str(art_id))
        if not raw:
            continue

        try:
            # Безопасное извлечение данных без жесткой валидации
            author_data = raw.get("author") or {}
            stats_data = raw.get("statistics") or {}
            
            content = raw.get("textHtml")
            if not content and raw.get("leadData"):
                content = raw["leadData"].get("textHtml", "")

            results.append(
                HabrPostBase(
                    habr_id=str(raw.get("id")),
                    title=client.clean_html(raw.get("titleHtml", "Без названия")),
                    content=client.clean_html(content or ""),
                    author=author_data.get("alias") or author_data.get("fullname") or "Unknown",
                    published_at=raw.get("timePublished"),
                    hubs=[h.get("title", "") for h in raw.get("hubs", [])],
                    tags=[client.clean_html(t.get("titleHtml", "")) for t in raw.get("tags", [])],
                    score=stats_data.get("score", 0),
                    reading_time=raw.get("readingTime", 0),
                    original_url=f"https://habr.com/ru/articles/{raw.get('id')}/",
                )
            )
        except Exception:
            continue

    return results