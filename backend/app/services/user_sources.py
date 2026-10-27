"""Attach and enable global sources for one user.

sources stays one row per channel. user_sources is the per-user link.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.connectors.base import InvalidSourceError
from app.connectors.habr.client import HabrClient
from app.connectors.telegram.client import normalize_channel_source
from app.connectors.telegram.errors import TelegramChannelAccessError
from app.connectors.youtube.urls import RSS_CHANNEL_RE, canonical_channel_url, parse_channel_url
from app.models.enums import SourceType
from app.models.source import Source
from app.models.user_source import UserSource
from app.schemas.catalog import SourceLinkResponse
from app.services.reddit import normalize_subreddit
from app.services.users import require_user

_EXTERNAL_ID_TYPES = frozenset({SourceType.YOUTUBE, SourceType.HABR, SourceType.REDDIT})


class InvalidSourceAttachError(ValueError):
    """The type and URL do not identify a catalog source."""


class UnknownSourceLinkError(LookupError):
    """This user has no user_sources row for the source."""


def list_sources(session: Session, user_id: int) -> list[SourceLinkResponse]:
    require_user(session, user_id)
    rows = session.execute(
        select(UserSource, Source)
        .join(Source, Source.id == UserSource.source_id)
        .where(UserSource.user_id == user_id)
        .order_by(UserSource.id)
    ).all()
    return [_link_response(link, source) for link, source in rows]


def attach_source(
    session: Session,
    user_id: int,
    source_type: SourceType,
    url: str,
    name: str | None,
) -> tuple[SourceLinkResponse, bool]:
    """Link the user to a global source. Returns the link and whether it is new."""
    require_user(session, user_id)
    identity = _identity(source_type, url, name)
    source = _get_or_create_source(session, identity)
    link, created = _get_or_create_link(session, user_id, source.id)
    response = _link_response(link, source)
    activated = created and link.enabled
    source_id = source.id
    _commit(session)
    if activated:
        _notify_source_activated(user_id, source_id)
    return response, created


def set_source_enabled(
    session: Session, user_id: int, source_id: int, enabled: bool
) -> SourceLinkResponse:
    """Flip user_sources.enabled. The shared sources row stays."""
    require_user(session, user_id)
    link = session.scalar(
        select(UserSource).where(
            UserSource.user_id == user_id,
            UserSource.source_id == source_id,
        )
    )
    if link is None:
        raise UnknownSourceLinkError(source_id)
    source = session.get(Source, source_id)
    if source is None:
        raise UnknownSourceLinkError(source_id)
    changed_to_enabled = enabled and not link.enabled
    link.enabled = enabled
    response = _link_response(link, source)
    _commit(session)
    if changed_to_enabled:
        _notify_source_activated(user_id, source_id)
    return response


def unlink_source(session: Session, user_id: int, source_id: int) -> None:
    """Remove only this user's link; shared source and publications remain."""
    require_user(session, user_id)
    link = session.scalar(
        select(UserSource).where(
            UserSource.user_id == user_id,
            UserSource.source_id == source_id,
        )
    )
    if link is None:
        raise UnknownSourceLinkError(source_id)
    session.delete(link)
    _commit(session)


def _identity(source_type: SourceType, url: str, name: str | None) -> _SourceIdentity:
    if source_type is SourceType.YOUTUBE:
        channel_id = _youtube_channel_id(url)
        return _SourceIdentity(
            type=source_type,
            name=_display_name(name, channel_id),
            canonical_url=canonical_channel_url(channel_id),
            external_id=channel_id,
        )
    if source_type is SourceType.HABR:
        canonical = _normalize_http_url(url)
        if canonical != _normalize_http_url(HabrClient.RSS_URL):
            raise InvalidSourceAttachError("Pass the shared Habr articles URL.")
        return _SourceIdentity(
            type=source_type,
            name=_display_name(name, "Habr"),
            canonical_url=HabrClient.RSS_URL,
            external_id="habr:articles",
        )
    if source_type is SourceType.REDDIT:
        try:
            reddit_input = url.strip()
            if "://" not in reddit_input:
                if re.fullmatch(r"/?(?:r/)?[A-Za-z0-9_]{1,21}/?", reddit_input):
                    subreddit = normalize_subreddit(reddit_input)
                    reddit_input = ""
                elif reddit_input.lower().startswith(
                    ("reddit.com/", "www.reddit.com/", "old.reddit.com/")
                ):
                    reddit_input = f"https://{reddit_input}"
                else:
                    raise ValueError("Pass a Reddit subreddit URL or name.")
            if reddit_input:
                parsed = urlparse(reddit_input)
                if (
                    parsed.scheme.lower() not in {"http", "https"}
                    or (parsed.hostname or "").lower()
                    not in {"reddit.com", "www.reddit.com", "old.reddit.com"}
                    or re.fullmatch(r"/r/[A-Za-z0-9_]{1,21}/?", parsed.path) is None
                    or parsed.query
                    or parsed.fragment
                ):
                    raise ValueError("Pass a Reddit subreddit URL or name.")
                subreddit = normalize_subreddit(reddit_input)
        except ValueError as exc:
            raise InvalidSourceAttachError(str(exc)) from exc
        return _SourceIdentity(
            type=source_type,
            name=_display_name(name, f"r/{subreddit}"),
            canonical_url=f"https://www.reddit.com/r/{subreddit}/",
            external_id=subreddit,
        )
    if source_type is SourceType.TELEGRAM:
        try:
            channel = normalize_channel_source(url)
        except TelegramChannelAccessError as exc:
            raise InvalidSourceAttachError(str(exc)) from exc
        canonical = str(channel) if isinstance(channel, int) else f"https://t.me/{channel.lower()}"
        return _SourceIdentity(
            type=source_type,
            name=_display_name(name, str(channel)),
            canonical_url=canonical,
            external_id=None,
        )
    canonical = _normalize_http_url(url)
    return _SourceIdentity(
        type=source_type,
        name=_display_name(name, _label_from_url(canonical)),
        canonical_url=canonical,
        external_id=None,
    )


def _youtube_channel_id(url: str) -> str:
    match = RSS_CHANNEL_RE.search(url or "")
    if match:
        return match.group(1)
    try:
        parsed = parse_channel_url(url)
    except InvalidSourceError as exc:
        raise InvalidSourceAttachError(str(exc)) from exc
    if parsed.channel_id:
        return parsed.channel_id
    raise InvalidSourceAttachError(
        "A YouTube URL must include a channel id, for example "
        "https://www.youtube.com/channel/UC.... This API does not fetch the channel."
    )


def _normalize_http_url(url: str) -> str:
    text = (url or "").strip()
    if not text:
        raise InvalidSourceAttachError("url is required")
    if "://" not in text:
        text = f"https://{text}"
    parsed = urlparse(text)
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower()
    if scheme not in {"http", "https"} or not host:
        raise InvalidSourceAttachError("url must be an http(s) address")
    port = parsed.port
    netloc = host
    if port and not ((scheme == "https" and port == 443) or (scheme == "http" and port == 80)):
        netloc = f"{host}:{port}"
    path = parsed.path or ""
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")
    query = f"?{parsed.query}" if parsed.query else ""
    return f"{scheme}://{netloc}{path}{query}"


def _label_from_url(canonical: str) -> str:
    parsed = urlparse(canonical)
    parts = [part for part in parsed.path.split("/") if part]
    return parts[-1] if parts else parsed.netloc


def _display_name(name: str | None, fallback: str) -> str:
    cleaned = (name or "").strip() or fallback.strip()
    if not cleaned:
        raise InvalidSourceAttachError("name is required")
    return cleaned[:255]


def _get_or_create_source(session: Session, identity: _SourceIdentity) -> Source:
    found = _find_source(session, identity)
    if found is not None:
        return found
    source = Source(
        type=identity.type,
        name=identity.name,
        canonical_url=identity.canonical_url,
        external_id=identity.external_id,
        created_at=_now(),
    )
    try:
        with session.begin_nested():
            session.add(source)
            session.flush()
    except IntegrityError:
        session.expunge(source)
        found = _find_source(session, identity)
        if found is None:
            raise
        return found
    return source


def _find_source(session: Session, identity: _SourceIdentity) -> Source | None:
    if identity.external_id is not None and identity.type in _EXTERNAL_ID_TYPES:
        statement = select(Source).where(
            Source.type == identity.type,
            Source.external_id == identity.external_id,
        )
    else:
        statement = select(Source).where(
            Source.type == identity.type,
            Source.canonical_url == identity.canonical_url,
        )
    return session.scalar(statement.order_by(Source.id).limit(1))


def _get_or_create_link(session: Session, user_id: int, source_id: int) -> tuple[UserSource, bool]:
    found = _find_link(session, user_id, source_id)
    if found is not None:
        return found, False
    link = UserSource(
        user_id=user_id,
        source_id=source_id,
        enabled=True,
        created_at=_now(),
    )
    try:
        with session.begin_nested():
            session.add(link)
            session.flush()
    except IntegrityError:
        session.expunge(link)
        found = _find_link(session, user_id, source_id)
        if found is None:
            raise
        return found, False
    return link, True


def _find_link(session: Session, user_id: int, source_id: int) -> UserSource | None:
    return session.scalar(
        select(UserSource).where(
            UserSource.user_id == user_id,
            UserSource.source_id == source_id,
        )
    )


def _link_response(link: UserSource, source: Source) -> SourceLinkResponse:
    return SourceLinkResponse(
        id=link.id,
        user_id=link.user_id,
        source_id=source.id,
        enabled=link.enabled,
        type=source.type,
        name=source.name,
        canonical_url=source.canonical_url,
        external_id=source.external_id,
        created_at=link.created_at,
    )


def _notify_source_activated(user_id: int, source_id: int) -> None:
    """Узкий хук: только фоновый пересчёт непросмотренного, без правок публикаций."""
    from app.services.user_relevance import notify_source_activated

    notify_source_activated(user_id, source_id)


def _commit(session: Session) -> None:
    try:
        session.commit()
    except Exception:
        session.rollback()
        raise


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class _SourceIdentity:
    def __init__(
        self,
        *,
        type: SourceType,
        name: str,
        canonical_url: str,
        external_id: str | None,
    ) -> None:
        self.type = type
        self.name = name
        self.canonical_url = canonical_url
        self.external_id = external_id
