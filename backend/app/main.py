from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

from app.api.digest import router as digest_router
from app.api.feed import router as feed_router
from app.api.feedback import router as feedback_router
from app.api.habr import router as habr_router
from app.api.health import router as health_router
from app.api.interests import router as interests_router
from app.api.users import sources_router, topics_router
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.db.session import get_engine


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging(get_settings().log_level)
    yield
    if get_engine.cache_info().currsize:
        get_engine().dispose()


app = FastAPI(
    title="Upivo Backend",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(health_router)
app.include_router(habr_router, prefix="/api")
app.include_router(sources_router, prefix="/api")
app.include_router(topics_router, prefix="/api")
app.include_router(interests_router, prefix="/api")
app.include_router(feedback_router, prefix="/api")
app.include_router(feed_router, prefix="/api")
app.include_router(digest_router, prefix="/api")

if __name__ == "__main__":
    settings = get_settings()
    uvicorn.run(
        "app.main:app",
        host=settings.app_host,
        port=settings.app_port,
        log_level=settings.log_level.lower(),
    )
