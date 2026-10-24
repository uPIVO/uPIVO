from __future__ import annotations

from sqlalchemy import BigInteger, CheckConstraint, ForeignKeyConstraint, UniqueConstraint
from sqlalchemy.dialects.postgresql import DOUBLE_PRECISION, JSONB

from app.models import ContentCluster, FeedItem, UserItemScore


def test_content_unit_ai_analysis_schema_supports_feed_items_and_clusters() -> None:
    cluster_columns = ContentCluster.__table__.columns
    assert isinstance(cluster_columns["importance_score"].type, DOUBLE_PRECISION)
    assert cluster_columns["importance_score"].nullable is True
    assert isinstance(cluster_columns["ai_metadata"].type, JSONB)
    assert cluster_columns["ai_metadata"].nullable is True
    feed_columns = FeedItem.__table__.columns
    assert isinstance(feed_columns["importance_score"].type, DOUBLE_PRECISION)
    assert feed_columns["importance_score"].nullable is True
    assert isinstance(feed_columns["ai_metadata"].type, JSONB)
    assert feed_columns["ai_metadata"].nullable is True

    table = UserItemScore.__table__
    columns = table.columns
    assert columns["feed_item_id"].nullable is True
    assert isinstance(columns["cluster_id"].type, BigInteger)
    assert columns["cluster_id"].nullable is True
    assert isinstance(columns["relevance_score"].type, DOUBLE_PRECISION)
    assert isinstance(columns["rank_score"].type, DOUBLE_PRECISION)
    assert "reason" in columns
    assert "calculated_at" in columns
    assert isinstance(columns["analysis_metadata"].type, JSONB)

    unique_constraints = {
        constraint.name: tuple(constraint.columns.keys())
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    assert unique_constraints["uq_user_item_scores_user_feed"] == ("user_id", "feed_item_id")
    assert unique_constraints["uq_user_item_scores_user_cluster"] == ("user_id", "cluster_id")

    checks = {
        constraint.name: "".join(str(constraint.sqltext).lower().split())
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint)
    }
    assert checks["ck_user_item_scores_exactly_one_target"] == (
        "(feed_item_idisnotnullandcluster_idisnull)or(feed_item_idisnullandcluster_idisnotnull)"
    )
    assert "ck_user_item_scores_relevance_range" in checks

    foreign_keys = {
        constraint.name: constraint
        for constraint in table.foreign_key_constraints
        if isinstance(constraint, ForeignKeyConstraint)
    }
    user_foreign_key = foreign_keys["fk_user_item_scores_user_id_users"]
    assert user_foreign_key.ondelete == "CASCADE"
    assert [element.target_fullname for element in user_foreign_key.elements] == ["users.id"]
    feed_foreign_key = foreign_keys["fk_user_item_scores_feed_item_id_feed_items"]
    assert feed_foreign_key.ondelete == "CASCADE"
    assert [element.target_fullname for element in feed_foreign_key.elements] == ["feed_items.id"]
    cluster_foreign_key = foreign_keys["fk_user_item_scores_cluster_id_content_clusters"]
    assert cluster_foreign_key.ondelete == "CASCADE"
    assert [element.target_fullname for element in cluster_foreign_key.elements] == [
        "content_clusters.id"
    ]

    indexes = {index.name: index for index in table.indexes}
    assert "idx_user_item_scores_feed_item_id" in indexes
    assert tuple(indexes["idx_user_item_scores_cluster_id"].columns.keys()) == ("cluster_id",)
