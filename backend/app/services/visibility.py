"""Единый предикат доступности единиц контента для пользовательской выдачи.

Правило использует и лента, и обратная связь, чтобы «что видно» и «что можно
оценить» не расходились. Это проверка *возможности показа* по включённому
источнику и статусу обработки, а не факт показа конкретной карточки: учёта
показов пока нет.
"""

from sqlalchemy import and_, exists, select
from sqlalchemy.sql.elements import ColumnElement

from app.models.content_cluster import ClusterItem, ContentCluster
from app.models.enums import ProcessingStatus
from app.models.feed_item import FeedItem
from app.models.user_source import UserSource

# Статусы, допустимые для карточки текущей ленты.
VISIBLE_PROCESSING_STATUSES = (ProcessingStatus.NORMALIZED, ProcessingStatus.PROCESSED)


def feed_item_visible(user_id: int) -> ColumnElement[bool]:
    """Публикация обработана и её источник включён у этого пользователя."""
    return and_(
        FeedItem.processing_status.in_(VISIBLE_PROCESSING_STATUSES),
        exists(
            select(UserSource.id).where(
                UserSource.user_id == user_id,
                UserSource.source_id == FeedItem.source_id,
                UserSource.enabled.is_(True),
            )
        ),
    )


def standalone_feed_item_visible(user_id: int) -> ColumnElement[bool]:
    """Одиночная публикация: доступна пользователю и не вошла в инфоповод."""
    return and_(
        feed_item_visible(user_id),
        ~exists(select(ClusterItem.id).where(ClusterItem.feed_item_id == FeedItem.id)),
    )


def cluster_visible(user_id: int) -> ColumnElement[bool]:
    """Инфоповод виден, если хотя бы один его участник доступен пользователю.

    Подписка на все источники кластера не требуется: сводка может включать и
    другие источники.
    """
    member = (
        select(ClusterItem.id)
        .join(FeedItem, FeedItem.id == ClusterItem.feed_item_id)
        .where(ClusterItem.cluster_id == ContentCluster.id, feed_item_visible(user_id))
        .correlate(ContentCluster)
    )
    return exists(member)
