"""Создать общие таблицы sources и feed_items.

Revision ID: 0002
Revises: 0001
"""

# ruff: noqa: I001
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    source_type = postgresql.ENUM(
        "telegram",
        "website",
        "rss",
        "habr",
        "youtube",
        name="source_type",
        create_type=False,
    )
    source_type.create(op.get_bind(), checkfirst=False)
    processing_status = postgresql.ENUM(
        "pending",
        "processing",
        "processed",
        "failed",
        name="processing_status",
        create_type=False,
    )
    processing_status.create(op.get_bind(), checkfirst=False)

    op.create_table(
        "sources",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column("type", source_type, nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("canonical_url", sa.Text(), nullable=False),
        sa.Column("external_id", sa.String(length=512), nullable=True),
        sa.Column("last_synced_at", sa.DateTime(timezone=False), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=False),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
    )
    op.create_index("idx_sources_type_external_id", "sources", ["type", "external_id"])

    op.create_table(
        "feed_items",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column("source_id", sa.BigInteger(), sa.ForeignKey("sources.id"), nullable=False),
        sa.Column("external_id", sa.String(length=512), nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("body_text", sa.Text(), nullable=False),
        sa.Column("original_url", sa.Text(), nullable=False),
        sa.Column("author", sa.String(length=255), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=False), nullable=True),
        sa.Column(
            "collected_at",
            sa.DateTime(timezone=False),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("raw_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("extracted_entities", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("importance_score", postgresql.DOUBLE_PRECISION(), nullable=True),
        sa.Column(
            "processing_status",
            processing_status,
            server_default=sa.text("'pending'"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=False),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("updated_at", sa.DateTime(timezone=False), nullable=True),
        sa.UniqueConstraint("source_id", "external_id", name="uq_feed_items_source_external"),
    )
    op.create_index("idx_feed_items_content_hash", "feed_items", ["content_hash"])
    op.create_index("idx_feed_items_published_at", "feed_items", ["published_at"])
    op.create_index("idx_feed_items_source_id", "feed_items", ["source_id"])


def downgrade() -> None:
    op.drop_index("idx_feed_items_source_id", table_name="feed_items")
    op.drop_index("idx_feed_items_published_at", table_name="feed_items")
    op.drop_index("idx_feed_items_content_hash", table_name="feed_items")
    op.drop_table("feed_items")
    op.drop_index("idx_sources_type_external_id", table_name="sources")
    op.drop_table("sources")
    postgresql.ENUM(name="processing_status").drop(op.get_bind(), checkfirst=False)
    postgresql.ENUM(name="source_type").drop(op.get_bind(), checkfirst=False)
