"""Обратная связь пользователя по публикациям и инфоповодам.

Revision ID: 0012_feedback
Revises: 0011_user_interests
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0012_feedback"
down_revision = "0011_user_interests"
branch_labels = None
depends_on = None

_REACTION_VALUES = ("useful", "not_interested")


def _reaction_type() -> postgresql.ENUM:
    """Тип строки реакции; сам тип создаётся отдельно в upgrade()."""
    return postgresql.ENUM(*_REACTION_VALUES, name="feedback_type", create_type=False)


def upgrade() -> None:
    postgresql.ENUM(*_REACTION_VALUES, name="feedback_type").create(op.get_bind(), checkfirst=True)
    op.create_table(
        "feedback",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("feed_item_id", sa.BigInteger(), nullable=True),
        sa.Column("cluster_id", sa.BigInteger(), nullable=True),
        sa.Column("reaction", _reaction_type(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=False),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("updated_at", sa.DateTime(timezone=False), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_feedback_user_id_users",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["feed_item_id"],
            ["feed_items.id"],
            name="fk_feedback_feed_item_id_feed_items",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["cluster_id"],
            ["content_clusters.id"],
            name="fk_feedback_cluster_id_content_clusters",
            ondelete="CASCADE",
        ),
        # Ровно одна единица на строку: одиночная публикация либо инфоповод.
        sa.CheckConstraint(
            "(feed_item_id IS NOT NULL AND cluster_id IS NULL) OR "
            "(feed_item_id IS NULL AND cluster_id IS NOT NULL)",
            name="ck_feedback_exactly_one_target",
        ),
        sa.UniqueConstraint("user_id", "feed_item_id", name="uq_feedback_user_feed_item"),
        sa.UniqueConstraint("user_id", "cluster_id", name="uq_feedback_user_cluster"),
    )
    op.create_index(
        "idx_feedback_user_created_at",
        "feedback",
        ["user_id", sa.text("created_at DESC"), sa.text("id DESC")],
    )
    op.create_index("idx_feedback_feed_item_id", "feedback", ["feed_item_id"])
    op.create_index("idx_feedback_cluster_id", "feedback", ["cluster_id"])


def downgrade() -> None:
    op.drop_index("idx_feedback_cluster_id", table_name="feedback")
    op.drop_index("idx_feedback_feed_item_id", table_name="feedback")
    op.drop_index("idx_feedback_user_created_at", table_name="feedback")
    op.drop_table("feedback")
    postgresql.ENUM(name="feedback_type").drop(op.get_bind(), checkfirst=True)
