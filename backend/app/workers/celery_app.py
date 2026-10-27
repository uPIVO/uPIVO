from celery import Celery

from app.core.config import get_settings

celery_app = Celery(
    "upivo",
    broker=get_settings().redis_url.get_secret_value(),
    include=["app.workers.tasks"],
)
celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    beat_schedule={},
    broker_connection_retry_on_startup=True,
)
