"""Create content_clusters and cluster_items.

Revision ID: 0006_content_clusters
Revises: 0005_source_uniqueness
"""

# ruff: noqa: I001
from alembic import op
import sqlalchemy as sa

revision = "0006_content_clusters"
down_revision = "0005_source_uniqueness"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "content_clusters",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=False),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
    )
    op.create_table(
        "cluster_items",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column(
            "cluster_id",
            sa.BigInteger(),
            sa.ForeignKey("content_clusters.id"),
            nullable=False,
        ),
        sa.Column(
            "feed_item_id",
            sa.BigInteger(),
            sa.ForeignKey("feed_items.id"),
            nullable=False,
        ),
        sa.Column(
            "is_primary",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.UniqueConstraint("cluster_id", "feed_item_id", name="uq_cluster_items_cluster_feed"),
    )
    op.create_index("idx_cluster_items_feed_item_id", "cluster_items", ["feed_item_id"])


def downgrade() -> None:
    op.drop_index("idx_cluster_items_feed_item_id", table_name="cluster_items")
    op.drop_table("cluster_items")
    op.drop_table("content_clusters")
