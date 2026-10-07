import asyncio
from fastapi import APIRouter, HTTPException, Query
from app.connectors.habr.client import HabrClient
from app.schemas.habr import HabrPostBase

router = APIRouter(prefix="/habr", tags=["Habr Parser"])


@router.get("/preview", response_model=list[HabrPostBase], summary="Парсинг полных статей с Хабра")
async def preview_habr_posts(
    limit: int = Query(default=3, ge=1, le=10, description="Количество постов (рекомендуется 3-5 для скорости)"),
):
    client = HabrClient()
    try:
        data = await client.get_articles_page(page=1)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Ошибка при обращении к Хабру: {e}")

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
    for art_id, full_content in zip(article_ids, full_texts):
        raw = article_refs.get(str(art_id))
        if not raw:
            continue

        try:
            author_data = raw.get("author") or {}
            stats_data = raw.get("statistics") or {}

            # Если полный текст загрузился — берем его, иначе превью
            if isinstance(full_content, str) and full_content:
                final_content = full_content
            else:
                final_content = client.clean_html(raw.get("textHtml") or "")

            results.append(
                HabrPostBase(
                    habr_id=str(raw.get("id")),
                    title=client.clean_html(raw.get("titleHtml", "Без названия")),
                    content=final_content,
                    author=author_data.get("alias") or author_data.get("fullname") or "Unknown",
                    published_at=raw.get("timePublished"),
                    hubs=[h.get("title", "") for h in raw.get("hubs", []) if isinstance(h, dict)],
                    tags=[client.clean_html(t.get("titleHtml", "")) for t in raw.get("tags", []) if isinstance(t, dict)],
                    score=stats_data.get("score", 0),
                    reading_time=raw.get("readingTime", 0),
                    original_url=f"https://habr.com/ru/articles/{raw.get('id')}/",
                )
            )
        except Exception:
            continue

    return results