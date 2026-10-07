from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.schemas.habr import FeedItemResponse, HabrSyncRequest, HabrSyncResponse
from app.services.habr import HabrService

router = APIRouter(prefix="/habr", tags=["Habr Parser"])


@router.post("/sync", response_model=HabrSyncResponse, summary="Запустить сбор и сохранить посты в локальную БД")
async def sync_habr_to_db(
    payload: HabrSyncRequest,
    db: AsyncSession = Depends(get_db),
):
    service = HabrService(db)
    return await service.sync_publications(pages=payload.pages)


@router.get("/saved", response_model=list[FeedItemResponse], summary="Посмотреть сохраненные статьи из локальной БД")
async def list_saved_habr_posts(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
):
    service = HabrService(db)
    return await service.get_saved_posts(limit=limit, offset=offset)