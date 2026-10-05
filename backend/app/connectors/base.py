from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol


class ConnectorError(Exception):
    """Base error for source connectors."""


class InvalidSourceError(ConnectorError):
    """URL or identifier is not a supported source."""


class SourceFetchError(ConnectorError):
    """Network or upstream failure while reading a source."""


@dataclass(frozen=True)
class RawPublication:
    """Raw record from a connector, before normalization / AI."""

    external_id: str
    original_url: str
    title: str | None
    body_text: str
    published_at: datetime | None
    author: str | None = None
    raw_payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class FetchResult:
    source_name: str
    source_external_id: str
    canonical_url: str
    items: list[RawPublication]


class SourceConnector(Protocol):
    """Fetches raw publications. Normalization into FeedItem semantics is a later stage."""

    def fetch(self, canonical_url: str) -> FetchResult: ...
