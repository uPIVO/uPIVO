import logging
import re
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from app.connectors.telegram.credentials import TelegramSessionCredentials
from app.connectors.telegram.errors import (
    TelegramAuthorizationError,
    TelegramChannelAccessError,
    TelegramConfigurationError,
    TelegramConnectorError,
)
from app.connectors.telegram.models import ResolvedTelegramChannel, TelegramPost

logger = logging.getLogger(__name__)
_USERNAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{4,31}$")
_MARKED_CHANNEL_ID = re.compile(r"^-100[0-9]+$")
_CHANNEL_ID_OFFSET = 1_000_000_000_000
_ACCESS_ERROR_NAMES = {
    "ChannelInvalidError",
    "ChannelPrivateError",
    "ChatAdminRequiredError",
    "PeerIdInvalidError",
    "UserBannedInChannelError",
    "UserNotParticipantError",
    "UsernameInvalidError",
    "UsernameNotOccupiedError",
}


def normalize_channel_source(source: str) -> str | int:
    """Accept a Telegram username, channel URL, or marked channel ID."""
    if not isinstance(source, str) or not source.strip():
        raise TelegramChannelAccessError(
            "Provide a Telegram username, channel URL, or signed channel ID."
        )

    value = source.strip()
    if _MARKED_CHANNEL_ID.fullmatch(value):
        marked_id = int(value)
        if marked_id < -_CHANNEL_ID_OFFSET:
            return marked_id
        raise TelegramChannelAccessError(
            "Use a full Telethon marked channel ID, for example -1001234567890."
        )

    if value.startswith("+") or value.lower().startswith("joinchat/"):
        raise TelegramChannelAccessError(
            "Invite links are not accepted; use a channel this account already belongs to."
        )

    if "/" in value or value.lower().startswith(("http://", "https://")):
        candidate = value if "://" in value else f"https://{value}"
        parsed = urlsplit(candidate)
        if parsed.scheme not in {"http", "https"} or parsed.hostname not in {
            "t.me",
            "www.t.me",
            "telegram.me",
            "www.telegram.me",
        }:
            raise TelegramChannelAccessError(
                "Use a Telegram username, a t.me channel URL, or a signed channel ID."
            )
        parts = [part for part in parsed.path.split("/") if part]
        if parts and parts[0].lower() == "joinchat":
            raise TelegramChannelAccessError(
                "Invite links are not accepted; use a channel this account already belongs to."
            )
        if parts and parts[0].startswith("+"):
            raise TelegramChannelAccessError(
                "Invite links are not accepted; use a channel this account already belongs to."
            )
        if len(parts) == 2 and parts[0].lower() == "s":
            parts = parts[1:]
        elif len(parts) == 2 and parts[0].lower() == "c" and parts[1].isdigit():
            channel_id = int(parts[1])
            if channel_id > 0:
                return -(_CHANNEL_ID_OFFSET + channel_id)
            raise TelegramChannelAccessError("The private channel URL has an invalid channel ID.")
        if len(parts) != 1:
            raise TelegramChannelAccessError(
                "Use a channel URL without a post path or an invite link."
            )
        value = parts[0]

    value = value.removeprefix("@").strip()
    if not _USERNAME.fullmatch(value):
        raise TelegramChannelAccessError(
            "Use a Telegram username, a t.me channel URL, or a signed channel ID."
        )
    return value


class TelegramConnector:
    """Read Telegram channels without joining, persisting state, or advancing cursors."""

    def __init__(self, *, client: Any | None = None, settings: Any | None = None):
        self._client = client
        self._settings = settings
        self._entered = False

    async def __aenter__(self) -> "TelegramConnector":
        if self._client is None:
            self._client = self._create_client()
        try:
            if not self._client.is_connected():
                await self._client.connect()
            if not await self._client.is_user_authorized():
                raise TelegramAuthorizationError(
                    "Telegram session is not authorized. Run the local session generator first."
                )
        except TelegramAuthorizationError:
            await self._disconnect_quietly()
            raise
        except Exception as exc:
            await self._disconnect_quietly()
            logger.warning("Telegram connection failed (error_type=%s)", type(exc).__name__)
            raise TelegramConnectorError(
                "Telegram connection failed; check local credentials and network access."
            ) from None
        self._entered = True
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        self._entered = False
        await self._disconnect_quietly()

    def _create_client(self) -> Any:
        settings = self._settings
        if settings is None:
            try:
                from app.core.config import get_settings

                settings = get_settings()
            except Exception:
                raise TelegramConfigurationError(
                    "Не удалось загрузить настройки Telegram из корневого .env."
                ) from None

        credentials = TelegramSessionCredentials.from_settings(settings)
        try:
            from telethon import TelegramClient
            from telethon.sessions import StringSession

            return TelegramClient(
                StringSession(credentials.session.get_secret_value()),
                credentials.api_id,
                credentials.api_hash.get_secret_value(),
            )
        except Exception:
            raise TelegramConfigurationError(
                "Не удалось создать клиент Telethon; проверьте локальные настройки Telegram."
            ) from None

    async def _disconnect_quietly(self) -> None:
        if self._client is None:
            return
        try:
            if self._client.is_connected():
                await self._client.disconnect()
        except Exception as exc:
            logger.warning("Telegram disconnect failed (error_type=%s)", type(exc).__name__)

    async def resolve_channel(
        self, source: str | ResolvedTelegramChannel
    ) -> ResolvedTelegramChannel:
        self._require_connected()
        if isinstance(source, ResolvedTelegramChannel):
            return source
        normalized = normalize_channel_source(source)
        try:
            entity = await self._client.get_entity(normalized)
        except Exception as exc:
            if not _is_channel_access_error(exc):
                raise TelegramConnectorError("Telegram channel resolution failed.") from None
            entity = None
            if isinstance(normalized, int):
                try:
                    entity = await self._find_channel_in_dialogs(normalized)
                except Exception as dialog_exc:
                    self._log_channel_access_failure(dialog_exc)
            if entity is None:
                self._log_channel_access_failure(exc)
                raise _channel_access_error() from None

        channel_id = getattr(entity, "id", None)
        is_channel = hasattr(entity, "broadcast") or hasattr(entity, "megagroup")
        if not isinstance(channel_id, int) or not is_channel:
            raise TelegramChannelAccessError("The resolved Telegram target is not a channel.")
        return ResolvedTelegramChannel(
            channel_id=channel_id,
            title=str(getattr(entity, "title", "") or ""),
            entity=entity,
        )

    async def fetch_latest_posts(
        self, source: str | ResolvedTelegramChannel, *, limit: int = 50
    ) -> list[TelegramPost]:
        self._require_connected()
        if not isinstance(limit, int) or limit <= 0:
            raise ValueError("limit must be a positive integer")
        channel = await self.resolve_channel(source)
        try:
            messages = self._client.iter_messages(channel.entity)
            return await _collect_logical_posts(messages, limit=limit)
        except Exception as exc:
            if not _is_channel_access_error(exc):
                raise TelegramConnectorError("Telegram post retrieval failed.") from None
            self._log_channel_access_failure(exc)
            raise _channel_access_error() from None

    async def fetch_new_posts(
        self,
        source: str | ResolvedTelegramChannel,
        *,
        after_message_id: int,
    ) -> list[TelegramPost]:
        self._require_connected()
        if not isinstance(after_message_id, int) or after_message_id < 0:
            raise ValueError("after_message_id must be a non-negative integer")
        channel = await self.resolve_channel(source)
        try:
            messages = self._client.iter_messages(channel.entity, min_id=after_message_id)
            posts = await _collect_logical_posts(messages)
            if posts and posts[0].grouped_id is not None:
                posts[0] = await _complete_boundary_album(self._client, channel.entity, posts[0])
            return posts
        except Exception as exc:
            if not _is_channel_access_error(exc):
                raise TelegramConnectorError("Telegram post retrieval failed.") from None
            self._log_channel_access_failure(exc)
            raise _channel_access_error() from None

    async def _find_channel_in_dialogs(self, marked_channel_id: int) -> Any | None:
        async for dialog in self._client.iter_dialogs():
            entity = getattr(dialog, "entity", None)
            channel_id = getattr(entity, "id", None)
            is_channel = hasattr(entity, "broadcast") or hasattr(entity, "megagroup")
            if not isinstance(channel_id, int) or not is_channel:
                continue
            dialog_id = getattr(dialog, "id", None)
            marked_id = -(_CHANNEL_ID_OFFSET + channel_id)
            if dialog_id == marked_channel_id or marked_id == marked_channel_id:
                return entity
        return None

    def _require_connected(self) -> None:
        if not self._entered or self._client is None:
            raise TelegramConnectorError("Use TelegramConnector inside an async context manager.")

    @staticmethod
    def _log_channel_access_failure(exc: Exception) -> None:
        logger.warning("Telegram channel access failed (error_type=%s)", type(exc).__name__)


async def _complete_boundary_album(client: Any, channel: Any, post: TelegramPost) -> TelegramPost:
    first_message_id = post.message_ids[0]
    earlier_messages = []
    async for message in client.iter_messages(channel, max_id=first_message_id):
        if (
            getattr(message, "action", None) is not None
            or not _is_post(message)
            or getattr(message, "grouped_id", None) != post.grouped_id
        ):
            break
        earlier_messages.append(message)
    if not earlier_messages:
        return post

    ordered_earlier = sorted(earlier_messages, key=lambda message: message.id)
    earlier_captions = []
    for message in ordered_earlier:
        text = getattr(message, "message", None) or getattr(message, "text", None) or ""
        if text.strip():
            earlier_captions.append(text.strip())
    message_ids = tuple(
        sorted(set(post.message_ids).union(message.id for message in ordered_earlier))
    )
    timestamps = [_as_utc(message.date) for message in ordered_earlier]
    text_parts = earlier_captions + ([post.text] if post.text else [])
    return replace(
        post,
        message_id=max(message_ids),
        message_ids=message_ids,
        text="\n".join(text_parts),
        published_at=min([post.published_at, *timestamps]),
        is_forward=post.is_forward
        or any(getattr(message, "fwd_from", None) is not None for message in ordered_earlier),
    )


async def _collect_logical_posts(messages: Any, *, limit: int | None = None) -> list[TelegramPost]:
    posts: list[TelegramPost] = []
    async for post in _logical_posts(messages):
        posts.append(post)
        if limit is not None and len(posts) >= limit:
            break
    return sorted(posts, key=lambda post: (post.message_id, post.external_id))


async def _logical_posts(messages: Any) -> AsyncIterator[TelegramPost]:
    album: list[Any] = []
    album_id: int | None = None

    async for message in messages:
        if getattr(message, "action", None) is not None or not _is_post(message):
            if album:
                yield _make_post(album, album_id)
                album = []
                album_id = None
            continue

        grouped_id = getattr(message, "grouped_id", None)
        if album and grouped_id == album_id and grouped_id is not None:
            album.append(message)
            continue
        if album:
            yield _make_post(album, album_id)
            album = []
            album_id = None

        if grouped_id is None:
            yield _make_post([message], None)
        else:
            album_id = int(grouped_id)
            album = [message]

    if album:
        yield _make_post(album, album_id)


def _is_post(message: Any) -> bool:
    if not isinstance(getattr(message, "id", None), int):
        return False
    text = getattr(message, "message", None) or getattr(message, "text", None) or ""
    return bool(text) or getattr(message, "media", None) is not None


def _make_post(messages: list[Any], grouped_id: int | None) -> TelegramPost:
    ordered = sorted(messages, key=lambda message: message.id)
    message_ids = tuple(message.id for message in ordered)
    captions = []
    for message in ordered:
        text = getattr(message, "message", None) or getattr(message, "text", None) or ""
        if text.strip():
            captions.append(text.strip())
    timestamps = [_as_utc(message.date) for message in ordered]
    is_album = grouped_id is not None
    return TelegramPost(
        external_id=f"album:{grouped_id}" if is_album else f"message:{message_ids[0]}",
        message_id=max(message_ids),
        text="\n".join(captions),
        published_at=min(timestamps),
        grouped_id=grouped_id,
        is_forward=any(getattr(message, "fwd_from", None) is not None for message in ordered),
        message_ids=message_ids,
    )


def _as_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise TelegramConnectorError("Telegram message is missing its publication timestamp.")
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _is_channel_access_error(exc: Exception) -> bool:
    return isinstance(exc, (ValueError, LookupError)) or (
        type(exc).__module__.startswith("telethon.errors")
        and type(exc).__name__ in _ACCESS_ERROR_NAMES
    )


def _channel_access_error() -> TelegramChannelAccessError:
    return TelegramChannelAccessError(
        "Cannot resolve or read this Telegram channel. "
        "Private channels are accessible only if this account is already a member."
    )
