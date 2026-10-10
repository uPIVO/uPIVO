"""Add normalization-complete state to feed item processing status.

Revision ID: 0004_normalized_status
Revises: 0003_reddit
"""

from alembic import op

revision = "0004_normalized_status"
down_revision = "0003_reddit"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # A new PostgreSQL enum value must be committed before it can be used.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE processing_status ADD VALUE IF NOT EXISTS 'normalized'")
    # No full processing pipeline existed before this migration: old "processed"
    # rows had only completed normalization.
    op.execute(
        "UPDATE feed_items SET processing_status = 'normalized' "
        "WHERE processing_status = 'processed'"
    )


def downgrade() -> None:
    # PostgreSQL cannot safely remove one enum value without rebuilding the type.
    pass
