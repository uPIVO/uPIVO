from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True, slots=True)
class TelegramPost:
    """One Telegram message or media album normalized as a logical post."""

    external_id: str
    message_id: int
    text: str
    published_at: datetime
    grouped_id: int | None = None
    is_forward: bool = False
    message_ids: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class ResolvedTelegramChannel:
    """A resolved channel with a stable Telegram ID and a hidden Telethon entity."""

    channel_id: int
    title: str
    entity: Any = field(repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class TelegramChannelInfo:
    """Название канала Telegram и его полный ID Telethon."""

    marked_id: int
    title: str
