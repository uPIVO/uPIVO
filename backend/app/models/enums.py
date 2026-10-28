from enum import StrEnum

from sqlalchemy import Enum as SAEnum


class SourceType(StrEnum):
    TELEGRAM = "telegram"
    WEBSITE = "website"
    RSS = "rss"
    HABR = "habr"
    YOUTUBE = "youtube"
    REDDIT = "reddit"


class ProcessingStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    NORMALIZED = "normalized"
    PROCESSED = "processed"
    FAILED = "failed"


class DigestType(StrEnum):
    GENERAL = "general"
    TOPIC = "topic"


class FeedbackType(StrEnum):
    """Явная реакция пользователя на единицу контента."""

    USEFUL = "useful"
    NOT_INTERESTED = "not_interested"


def pg_enum(enum_cls: type[StrEnum], name: str) -> SAEnum:
    return SAEnum(
        enum_cls,
        name=name,
        native_enum=True,
        values_callable=lambda members: [item.value for item in members],
        validate_strings=True,
    )


source_type_enum = pg_enum(SourceType, "source_type")
processing_status_enum = pg_enum(ProcessingStatus, "processing_status")
digest_type_enum = pg_enum(DigestType, "digest_type")
feedback_type_enum = pg_enum(FeedbackType, "feedback_type")
