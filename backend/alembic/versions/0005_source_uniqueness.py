"""Ensure YouTube and Habr external IDs identify one shared source.

Revision ID: 0005_source_uniqueness
Revises: 0004_normalized_status
"""

import sqlalchemy as sa

from alembic import op

revision = "0005_source_uniqueness"
down_revision = "0004_normalized_status"
branch_labels = None
depends_on = None


def upgrade() -> None:
    missing = op.get_bind().execute(
        sa.text(
            "SELECT type, array_agg(id ORDER BY id) AS ids FROM sources "
            "WHERE type IN ('youtube', 'habr') AND external_id IS NULL "
            "GROUP BY type ORDER BY type"
        )
    ).all()
    if missing:
        details = "; ".join(f"{source_type}: source IDs {ids}" for source_type, ids in missing)
        raise RuntimeError(
            "Cannot require YouTube/Habr external_id: existing sources have NULL IDs: "
            + details
        )

    duplicates = op.get_bind().execute(
        sa.text(
            "SELECT type, external_id, array_agg(id ORDER BY id) AS ids "
            "FROM sources WHERE type IN ('youtube', 'habr') AND external_id IS NOT NULL "
            "GROUP BY type, external_id HAVING count(*) > 1 "
            "ORDER BY type, external_id LIMIT 5"
        )
    ).all()
    if duplicates:
        details = "; ".join(
            f"{source_type}/{external_id}: source IDs {ids}"
            for source_type, external_id, ids in duplicates
        )
        raise RuntimeError(
            "Cannot add YouTube/Habr source uniqueness: duplicate external_id rows exist: "
            + details
        )

    op.create_check_constraint(
        "ck_sources_youtube_habr_external_id_required",
        "sources",
        "type NOT IN ('youtube', 'habr') OR external_id IS NOT NULL",
    )

    op.create_index(
        "uq_sources_youtube_external_id",
        "sources",
        ["external_id"],
        unique=True,
        postgresql_where=sa.text("type = 'youtube'"),
    )
    op.create_index(
        "uq_sources_habr_external_id",
        "sources",
        ["external_id"],
        unique=True,
        postgresql_where=sa.text("type = 'habr'"),
    )


def downgrade() -> None:
    op.drop_constraint("ck_sources_youtube_habr_external_id_required", "sources", type_="check")
    op.drop_index("uq_sources_habr_external_id", table_name="sources")
    op.drop_index("uq_sources_youtube_external_id", table_name="sources")
