"""Add thematic folders for the shared feed.

Revision ID: 0006_folders
Revises: 0005_source_uniqueness
"""

import sqlalchemy as sa

from alembic import op

revision = "0006_folders"
down_revision = "0005_source_uniqueness"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "folders",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("parent_id", sa.BigInteger(), sa.ForeignKey("folders.id"), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=False),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("updated_at", sa.DateTime(timezone=False), nullable=True),
    )
    op.create_index("idx_folders_user_id", "folders", ["user_id"])
    op.create_index("idx_folders_parent_id", "folders", ["parent_id"])

    op.create_table(
        "feed_item_folders",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column("folder_id", sa.BigInteger(), sa.ForeignKey("folders.id"), nullable=False),
        sa.Column("feed_item_id", sa.BigInteger(), sa.ForeignKey("feed_items.id"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=False),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.UniqueConstraint("folder_id", "feed_item_id", name="uq_feed_item_folders_folder_feed"),
    )
    op.create_index("idx_feed_item_folders_feed_item_id", "feed_item_folders", ["feed_item_id"])


def downgrade() -> None:
    op.drop_index("idx_feed_item_folders_feed_item_id", table_name="feed_item_folders")
    op.drop_table("feed_item_folders")
    op.drop_index("idx_folders_parent_id", table_name="folders")
    op.drop_index("idx_folders_user_id", table_name="folders")
    op.drop_table("folders")
