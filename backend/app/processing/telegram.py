"""Чистое преобразование Telegram-сущностей в общие поля публикаций."""

from datetime import UTC
from typing import Any

from telethon.tl.types import PeerChannel
from telethon.utils import get_peer_id

from app.connectors.telegram.models import ResolvedTelegramChannel, TelegramPost
from app.models import SourceType


def map_channel(channel: ResolvedTelegramChannel) -> dict[str, Any]:
    """Преобразовать канал в поля общей модели источника."""
    username = getattr(channel.entity, "username", None)
    canonical_url = (
        f"https://t.me/{username}" if username else f"https://t.me/c/{channel.channel_id}"
    )
    return {
        "type": SourceType.TELEGRAM,
        "name": " ".join(channel.title.split()) or "Канал без названия",
        "canonical_url": canonical_url,
        "external_id": str(get_peer_id(PeerChannel(channel.channel_id))),
    }


def map_post(channel: ResolvedTelegramChannel, post: TelegramPost) -> dict[str, Any]:
    """Преобразовать логическую публикацию, включая альбом, в поля feed_items."""
    message_ids = tuple(post.message_ids) or (post.message_id,)
    first_message_id = min(message_ids)
    username = getattr(channel.entity, "username", None)
    channel_url = (
        f"https://t.me/{username}" if username else f"https://t.me/c/{channel.channel_id}"
    )
    url = f"{channel_url}/{first_message_id}"
    timestamp = post.published_at
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)
    timestamp_utc = timestamp.astimezone(UTC)
    raw_payload = {
        "body_text": post.text,
        "text": post.text,
        "original_url": url,
        "url": url,
        "published_at": timestamp_utc.isoformat(),
        "grouped_id": post.grouped_id,
        "message_id": post.message_id,
        "message_ids": list(message_ids),
        "is_forward": post.is_forward,
        "ai_eligible": bool(post.text.strip()),
    }
    return {
        "external_id": post.external_id,
        "title": None,
        "body_text": post.text,
        "original_url": url,
        "author": None,
        "published_at": timestamp_utc.replace(tzinfo=None),
        "raw_payload": raw_payload,
    }
