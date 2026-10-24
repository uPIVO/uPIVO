"""Create content_clusters and cluster_items.

Revision ID: 0006_content_clusters
Revises: 01add98b80ff
"""

# ruff: noqa: I001
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0006_content_clusters"
down_revision = "01add98b80ff"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "content_clusters",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("importance_score", sa.Float(), nullable=True),
        sa.Column("ai_metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
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
            sa.ForeignKey("content_clusters.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "feed_item_id",
            sa.BigInteger(),
            sa.ForeignKey("feed_items.id", ondelete="CASCADE"),
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

    # Add cluster_id column to digest_items
    op.add_column(
        "digest_items",
        sa.Column(
            "cluster_id",
            sa.BigInteger(),
            sa.ForeignKey("content_clusters.id", ondelete="CASCADE"),
            nullable=True,
        ),
    )
    op.create_index("idx_digest_items_cluster_id", "digest_items", ["cluster_id"])


def downgrade() -> None:
    op.drop_index("idx_digest_items_cluster_id", table_name="digest_items")
    op.drop_column("digest_items", "cluster_id")
    op.drop_index("idx_cluster_items_feed_item_id", table_name="cluster_items")
    op.drop_table("cluster_items")
    op.drop_table("content_clusters")
