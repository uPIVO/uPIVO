"""Добавить промежуточное хранение анализа для FeedItem.

Revision ID: 0006_feed_item_ai_analysis
Revises: 0005_source_uniqueness
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0006_feed_item_ai_analysis"
down_revision = "0005_source_uniqueness"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "feed_items",
        sa.Column("ai_metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.create_table(
        "user_item_scores",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column(
            "user_id",
            sa.BigInteger(),
            sa.ForeignKey(
                "users.id",
                name="fk_user_item_scores_user_id_users",
                ondelete="CASCADE",
            ),
            nullable=False,
        ),
        sa.Column(
            "feed_item_id",
            sa.BigInteger(),
            sa.ForeignKey(
                "feed_items.id",
                name="fk_user_item_scores_feed_item_id_feed_items",
                ondelete="CASCADE",
            ),
            nullable=False,
        ),
        sa.Column("relevance_score", postgresql.DOUBLE_PRECISION(), nullable=True),
        sa.Column("rank_score", postgresql.DOUBLE_PRECISION(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column(
            "calculated_at",
            sa.DateTime(timezone=False),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("analysis_metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.CheckConstraint(
            "relevance_score IS NULL OR (relevance_score >= 0.0 AND relevance_score <= 1.0)",
            name="ck_user_item_scores_relevance_range",
        ),
        sa.UniqueConstraint("user_id", "feed_item_id", name="uq_user_item_scores_user_feed"),
    )
    op.create_index("idx_user_item_scores_feed_item_id", "user_item_scores", ["feed_item_id"])


def downgrade() -> None:
    op.drop_index("idx_user_item_scores_feed_item_id", table_name="user_item_scores")
    op.drop_table("user_item_scores")
    op.drop_column("feed_items", "ai_metadata")
