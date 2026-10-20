"""Добавить анализ ContentCluster и поддержку кластерных оценок.

Revision ID: 0009_content_unit_ai_analysis
Revises: 0008_cluster_item_constraints
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0009_content_unit_ai_analysis"
down_revision = "0008_cluster_item_constraints"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "content_clusters",
        sa.Column("importance_score", postgresql.DOUBLE_PRECISION(), nullable=True),
    )
    op.add_column(
        "content_clusters",
        sa.Column("ai_metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )

    op.alter_column(
        "user_item_scores",
        "feed_item_id",
        existing_type=sa.BigInteger(),
        existing_nullable=False,
        nullable=True,
    )
    op.add_column(
        "user_item_scores",
        sa.Column("cluster_id", sa.BigInteger(), nullable=True),
    )
    op.create_foreign_key(
        "fk_user_item_scores_cluster_id_content_clusters",
        "user_item_scores",
        "content_clusters",
        ["cluster_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_check_constraint(
        "ck_user_item_scores_exactly_one_target",
        "user_item_scores",
        "(feed_item_id IS NOT NULL AND cluster_id IS NULL) OR "
        "(feed_item_id IS NULL AND cluster_id IS NOT NULL)",
    )
    op.create_unique_constraint(
        "uq_user_item_scores_user_cluster",
        "user_item_scores",
        ["user_id", "cluster_id"],
    )
    op.create_index("idx_user_item_scores_cluster_id", "user_item_scores", ["cluster_id"])


def downgrade() -> None:
    bind = op.get_bind()
    has_cluster_scores = bind.execute(
        sa.text("SELECT EXISTS (SELECT 1 FROM user_item_scores WHERE cluster_id IS NOT NULL)")
    ).scalar()
    if has_cluster_scores:
        raise RuntimeError(
            "Нельзя отменить миграцию 0009: существуют оценки, привязанные к кластерам; "
            "миграция не удаляет и не преобразует эти оценки."
        )

    op.drop_index("idx_user_item_scores_cluster_id", table_name="user_item_scores")
    op.drop_constraint("uq_user_item_scores_user_cluster", "user_item_scores", type_="unique")
    op.drop_constraint("ck_user_item_scores_exactly_one_target", "user_item_scores", type_="check")
    op.drop_constraint(
        "fk_user_item_scores_cluster_id_content_clusters",
        "user_item_scores",
        type_="foreignkey",
    )
    op.drop_column("user_item_scores", "cluster_id")
    op.alter_column(
        "user_item_scores",
        "feed_item_id",
        existing_type=sa.BigInteger(),
        existing_nullable=True,
        nullable=False,
    )

    op.drop_column("content_clusters", "ai_metadata")
    op.drop_column("content_clusters", "importance_score")
