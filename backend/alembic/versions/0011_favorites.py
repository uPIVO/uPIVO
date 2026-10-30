"""Add favorites table for user saved items and clusters.

Revision ID: 0011_favorites
Revises: 0010_digest_items_snapshot
"""

import sqlalchemy as sa

from alembic import op

revision = "0011_favorites"
down_revision = "0010_digest_items_snapshot"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "favorites",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column(
            "user_id",
            sa.BigInteger(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "target_type",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'feed_item'"),
        ),
        sa.Column(
            "feed_item_id",
            sa.BigInteger(),
            sa.ForeignKey("feed_items.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "cluster_id",
            sa.BigInteger(),
            sa.ForeignKey("content_clusters.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=False),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "(target_type = 'feed_item' AND feed_item_id IS NOT NULL AND cluster_id IS NULL) OR "
            "(target_type = 'cluster' AND cluster_id IS NOT NULL AND feed_item_id IS NULL)",
            name="chk_favorites_target_reference",
        ),
        sa.UniqueConstraint(
            "user_id", "target_type", "feed_item_id", name="uq_favorites_user_feed_item"
        ),
        sa.UniqueConstraint(
            "user_id", "target_type", "cluster_id", name="uq_favorites_user_cluster"
        ),
    )
    op.create_index(
        "idx_favorites_user_id_created_at",
        "favorites",
        ["user_id", sa.text("created_at DESC"), sa.text("id DESC")],
    )
    op.create_index("idx_favorites_feed_item_id", "favorites", ["feed_item_id"])
    op.create_index("idx_favorites_cluster_id", "favorites", ["cluster_id"])


def downgrade() -> None:
    op.drop_index("idx_favorites_cluster_id", table_name="favorites")
    op.drop_index("idx_favorites_feed_item_id", table_name="favorites")
    op.drop_index("idx_favorites_user_id_created_at", table_name="favorites")
    op.drop_table("favorites")
