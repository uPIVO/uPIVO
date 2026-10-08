from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any


def parse_utc(value: int | float | str) -> datetime:
    if isinstance(value, str):
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        result = datetime.fromtimestamp(value, UTC)
    if result.tzinfo is None:
        result = result.replace(tzinfo=UTC)
    return result.astimezone(UTC).replace(tzinfo=None)


@dataclass(frozen=True)
class RedditPost:
    id: str
    title: str
    selftext: str
    permalink: str
    author: str | None
    created_utc: datetime
    raw_payload: dict[str, Any]

    @classmethod
    def from_payload(cls, raw: dict[str, Any]) -> "RedditPost":
        pid = str(raw.get("id", "")).strip()
        if not pid:
            raise ValueError("Публикация Reddit не содержит ID")
        path = str(raw.get("permalink", ""))
        url = path if path.startswith("https://") else f"https://www.reddit.com{path}"
        return cls(
            id=pid,
            title=str(raw.get("title") or ""),
            selftext=str(raw.get("selftext") or ""),
            permalink=url,
            author=str(raw["author"]) if raw.get("author") else None,
            created_utc=parse_utc(raw["created_utc"]),
            raw_payload=raw,
        )

    @property
    def body_text(self) -> str:
        return self.selftext or self.title

    @property
    def is_archived_removed(self) -> bool:
        placeholders = {"[deleted]", "[removed]"}
        category = self.raw_payload.get("removed_by_category")
        metadata = self.raw_payload.get("_meta")
        deleted_later = (
            isinstance(metadata, dict) and metadata.get("was_deleted_later") is True
        )
        has_category = bool(category) and (not isinstance(category, str) or bool(category.strip()))
        return (
            self.title.strip().casefold() in placeholders
            or self.selftext.strip().casefold() in placeholders
            or has_category
            or deleted_later
        )


@dataclass(frozen=True)
class RedditFetchBatch:
    """Подходящие публикации и максимальное время просмотренного ответа архива."""

    posts: list[RedditPost]
    scanned_through: datetime | None
    inspected_count: int
