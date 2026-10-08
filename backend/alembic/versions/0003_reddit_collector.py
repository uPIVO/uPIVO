"""Добавить состояние синхронизации Reddit.

Revision ID: 0003_reddit
Revises: 0002
"""

import sqlalchemy as sa

from alembic import op

revision = "0003_reddit"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # PostgreSQL запрещает использовать только что добавленное значение enum
    # в той же транзакции, поэтому сначала фиксируем изменение enum отдельно.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE source_type ADD VALUE IF NOT EXISTS 'reddit'")
    op.create_index(
        "uq_sources_reddit_external_id",
        "sources",
        ["external_id"],
        unique=True,
        postgresql_where=sa.text("type = 'reddit'"),
    )
    op.create_table(
        "reddit_sync_state",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column(
            "source_id",
            sa.BigInteger(),
            sa.ForeignKey("sources.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("cursor_at", sa.DateTime(timezone=False)),
        sa.Column("initial_floor_at", sa.DateTime(timezone=False)),
        sa.Column("overlap_seconds", sa.Integer(), server_default=sa.text("3600"), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=False),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_table("reddit_sync_state")
    op.drop_index("uq_sources_reddit_external_id", table_name="sources")
    # Значение PostgreSQL enum нельзя удалить безопасно без пересоздания типа.
