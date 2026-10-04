from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

from app.api.health import router
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.db.session import get_engine


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging(get_settings().log_level)
    yield
    if get_engine.cache_info().currsize:
        get_engine().dispose()


app = FastAPI(title="Upivo Backend", version="0.1.0", lifespan=lifespan)
app.include_router(router)

if __name__ == "__main__":
    settings = get_settings()
    uvicorn.run(
        "app.main:app",
        host=settings.app_host,
        port=settings.app_port,
        log_level=settings.log_level.lower(),
    )
