from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.enums import FavoriteTargetType
from app.schemas.feed import FeedItemListItem


class FavoriteCreateRequest(BaseModel):
    """Payload to add an item or cluster to favorites."""

    target_type: FavoriteTargetType = Field(
        default=FavoriteTargetType.FEED_ITEM,
        description="Тип сохраняемого объекта: feed_item или cluster",
    )
    target_id: int | None = Field(
        default=None,
        ge=1,
        description="ID публикации (feed_item_id) или инфоповода (cluster_id)",
    )
    feed_item_id: int | None = Field(
        default=None,
        ge=1,
        description="Обратная совместимость со старой схемой ITD-25: ID FeedItem",
    )
    cluster_id: int | None = Field(
        default=None,
        ge=1,
        description="ID кластера инфоповода",
    )
    note: str | None = Field(
        default=None,
        description="Пользовательская текстовая заметка к материалу",
    )

    @model_validator(mode="after")
    def validate_targets(self) -> "FavoriteCreateRequest":
        # Resolve target_id from specific fields if provided
        if self.target_id is None:
            if self.cluster_id is not None:
                self.target_type = FavoriteTargetType.CLUSTER
                self.target_id = self.cluster_id
            elif self.feed_item_id is not None:
                self.target_type = FavoriteTargetType.FEED_ITEM
                self.target_id = self.feed_item_id
            else:
                raise ValueError("Must provide either target_id, feed_item_id, or cluster_id")
        return self


class FavoriteUpdateRequest(BaseModel):
    """Payload to update favorite item note."""

    note: str | None = Field(
        description="Новая заметка (null очищает заметку, пустая строка допустима)"
    )


class FavoriteItemResponse(BaseModel):
    """One favorite entry for user."""

    id: int = Field(description="ID записи в таблице favorites")
    user_id: int = Field(description="ID владельца избранного")
    target_type: FavoriteTargetType = Field(description="Тип избранного: feed_item или cluster")
    target_id: int = Field(description="ID сохраненного FeedItem или ContentCluster")
    feed_item_id: int | None = Field(default=None, description="ID FeedItem, если это публикация")
    cluster_id: int | None = Field(
        default=None, description="ID ContentCluster, если это инфоповод"
    )
    note: str | None = Field(default=None, description="Пользовательская заметка")
    created_at: datetime = Field(description="Дата и время сохранения в избранное")
    card: FeedItemListItem = Field(description="Карточка материала в формате ленты Feed")

    model_config = ConfigDict(from_attributes=True)


class PaginatedFavoritesResponse(BaseModel):
    """Paginated list of user favorites."""

    items: list[FavoriteItemResponse]
    total: int
    limit: int
    offset: int
