"""Интересы пользователя для персональной оценки релевантности.

Revision ID: 0011_user_interests
Revises: 0010_digest_items_snapshot
"""

import sqlalchemy as sa

from alembic import op

revision = "0011_user_interests"
down_revision = "0010_digest_items_snapshot"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_interests",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
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
            name="fk_user_interests_user_id_users",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint("length(btrim(text)) > 0", name="ck_user_interests_text_not_blank"),
    )
    op.create_index("idx_user_interests_user_id", "user_interests", ["user_id"])

    # Уникальность проверяет сама БД: регистр и крайние пробелы не создают новый
    # интерес даже при конкурентных запросах, а не только предварительный SELECT.
    op.create_index(
        "uq_user_interests_user_text_lower",
        "user_interests",
        ["user_id", sa.text("lower(btrim(text))")],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_user_interests_user_text_lower", table_name="user_interests")
    op.drop_index("idx_user_interests_user_id", table_name="user_interests")
    op.drop_table("user_interests")
