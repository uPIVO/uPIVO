"""User digest settings.

Revision ID: 0015_digest_settings
Revises: 0014_sprint3_heads
Create Date: 2026-10-30 20:30:00.000000
"""

import sqlalchemy as sa

from alembic import op

revision = "0015_digest_settings"
down_revision = "0014_sprint3_heads"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_digest_settings",
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "period_hours",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("24"),
        ),
        sa.Column(
            "max_items",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("15"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=False),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=False),
            nullable=True,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_user_digest_settings_user_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("user_id", name="pk_user_digest_settings"),
        sa.CheckConstraint(
            "period_hours >= 1 AND period_hours <= 168",
            name="chk_user_digest_settings_period_hours",
        ),
        sa.CheckConstraint(
            "max_items >= 1 AND max_items <= 100",
            name="chk_user_digest_settings_max_items",
        ),
    )


def downgrade() -> None:
    op.drop_table("user_digest_settings")

