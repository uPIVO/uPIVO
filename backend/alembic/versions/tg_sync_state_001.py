"""Добавить курсор сохранения публикаций Telegram.

Revision ID: tg_sync_state_001
Revises: 0003
"""

import sqlalchemy as sa

from alembic import op

revision = "tg_sync_state_001"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "uq_sources_telegram_external_id",
        "sources",
        ["type", "external_id"],
        unique=True,
        postgresql_where=sa.text("type = 'telegram'"),
    )
    op.create_table(
        "telegram_sync_state",
        sa.Column(
            "source_id",
            sa.BigInteger(),
            sa.ForeignKey("sources.id"),
            primary_key=True,
        ),
        sa.Column("last_message_id", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.CheckConstraint(
            "last_message_id >= 0", name="ck_telegram_sync_state_last_message_id"
        ),
    )


def downgrade() -> None:
    op.drop_table("telegram_sync_state")
    op.drop_index("uq_sources_telegram_external_id", table_name="sources")
