"""ORM models. Imported here so Alembic sees metadata."""

from app.models.content_cluster import ClusterItem, ContentCluster
from app.models.digest import Digest, DigestItem
from app.models.digest_setting import UserDigestSetting
from app.models.enums import (
    DigestType,
    FavoriteTargetType,
    FeedbackType,
    ProcessingStatus,
    SourceType,
)
from app.models.favorite import Favorite
from app.models.feed_item import FeedItem
from app.models.feed_item_folder import FeedItemFolder
from app.models.feedback import Feedback
from app.models.folder import Folder
from app.models.reddit_sync_state import RedditSyncState
from app.models.source import Source
from app.models.telegram_sync_state import TelegramSyncState
from app.models.user import User
from app.models.user_interest import UserInterest
from app.models.user_item_score import UserItemScore
from app.models.user_source import UserSource

__all__ = [
    "ClusterItem",
    "ContentCluster",
    "Digest",
    "DigestItem",
    "DigestType",
    "UserDigestSetting",
    "Feedback",
    "FeedbackType",
    "Favorite",
    "FavoriteTargetType",
    "FeedItem",
    "FeedItemFolder",
    "Folder",
    "ProcessingStatus",
    "RedditSyncState",
    "Source",
    "SourceType",
    "TelegramSyncState",
    "User",
    "UserInterest",
    "UserItemScore",
    "UserSource",
]
