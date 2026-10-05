"""Source connectors. Concrete fetchers live in subpackages."""

from app.connectors.base import (
    ConnectorError,
    FetchResult,
    InvalidSourceError,
    RawPublication,
    SourceConnector,
    SourceFetchError,
)

__all__ = [
    "ConnectorError",
    "FetchResult",
    "InvalidSourceError",
    "RawPublication",
    "SourceConnector",
    "SourceFetchError",
]
