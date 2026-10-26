"""Store the rendered digest cards as a creation-time snapshot.

Revision ID: 0010_digest_items_snapshot
Revises: 0009_content_unit_ai_analysis
"""

import sqlalchemy as sa
from sqlalchemy.engine import RowMapping

from alembic import op

revision = "0010_digest_items_snapshot"
down_revision = "0009_content_unit_ai_analysis"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("digests", sa.Column("items_snapshot", sa.JSON(), nullable=True))

    # Historical cards cannot be recovered as they appeared on their creation
    # date. Freeze their currently visible content during this upgrade instead.
    bind = op.get_bind()
    digests = sa.table(
        "digests",
        sa.column("id", sa.BigInteger()),
        sa.column("items_snapshot", sa.JSON()),
    )
    digest_ids = bind.execute(sa.text("SELECT id FROM digests")).scalars().all()
    for digest_id in digest_ids:
        rows = bind.execute(
            sa.text(
                "SELECT fi.id AS item_id, fi.title AS item_title, "
                "fi.summary AS item_summary, fi.body_text, fi.original_url, fi.published_at, "
                "s.id AS source_id, s.name AS source_name, CAST(s.type AS TEXT) AS source_type, "
                "ci.cluster_id, ci.is_primary, cc.title AS cluster_title, "
                "cc.summary AS cluster_summary "
                "FROM digest_items di "
                "JOIN feed_items fi ON fi.id = di.feed_item_id "
                "JOIN sources s ON s.id = fi.source_id "
                "LEFT JOIN cluster_items ci ON ci.feed_item_id = fi.id "
                "LEFT JOIN content_clusters cc ON cc.id = ci.cluster_id "
                "WHERE di.digest_id = :digest_id "
                "ORDER BY fi.importance_score DESC NULLS LAST, "
                "fi.published_at DESC NULLS LAST"
            ),
            {"digest_id": digest_id},
        ).mappings().all()
        cards = _snapshot_cards(rows)
        bind.execute(
            sa.update(digests)
            .where(digests.c.id == digest_id)
            .values(items_snapshot=cards)
        )

    op.alter_column("digests", "items_snapshot", existing_type=sa.JSON(), nullable=False)


def _snapshot_cards(rows: list[RowMapping]) -> list[dict]:
    grouped: list[list[RowMapping]] = []
    cluster_positions: dict[int, int] = {}
    for row in rows:
        cluster_id = row["cluster_id"]
        if cluster_id is not None and cluster_id in cluster_positions:
            grouped[cluster_positions[cluster_id]].append(row)
        else:
            if cluster_id is not None:
                cluster_positions[cluster_id] = len(grouped)
            grouped.append([row])

    cards: list[dict] = []
    for members in grouped:
        primary = next((row for row in members if row["is_primary"]), members[0])
        cluster_id = primary["cluster_id"]
        title = primary["cluster_title"] if cluster_id else None
        summary = primary["cluster_summary"] if cluster_id else None
        if summary:
            summary = summary.strip()
        if not title:
            title = primary["item_title"] or "Без названия"
        if not summary:
            summary = primary["item_summary"]
            if not summary:
                body = (primary["body_text"] or "").strip()
                summary = body[:250] + "..." if len(body) > 250 else body

        sources = []
        seen_source_ids = set()
        for row in members:
            if row["source_id"] not in seen_source_ids:
                seen_source_ids.add(row["source_id"])
                sources.append(
                    {
                        "id": row["source_id"],
                        "name": row["source_name"],
                        "type": row["source_type"],
                    }
                )

        cards.append(
            {
                "id": primary["item_id"],
                "title": title.strip(),
                "summary": summary,
                "published_at": (
                    primary["published_at"].isoformat() if primary["published_at"] else None
                ),
                "source": {
                    "id": primary["source_id"],
                    "name": primary["source_name"],
                    "type": primary["source_type"],
                },
                "original_url": primary["original_url"],
                "is_cluster": cluster_id is not None,
                "cluster_id": cluster_id,
                "items_count": len(members),
                "related_sources": sources,
            }
        )
    return cards


def downgrade() -> None:
    has_digests = op.get_bind().execute(sa.text("SELECT EXISTS (SELECT 1 FROM digests)")).scalar()
    if has_digests:
        raise RuntimeError(
            "Нельзя отменить миграцию снимков: существующие дайджесты потеряют "
            "неизменяемое содержимое. Удалите их явно перед downgrade."
        )
    op.drop_column("digests", "items_snapshot")
