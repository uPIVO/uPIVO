"""Require exclusive cluster membership and at most one primary per cluster.

Revision ID: 0008_cluster_item_constraints
Revises: 0007_integrated_heads
"""

import sqlalchemy as sa

from alembic import op

revision = "0008_cluster_item_constraints"
down_revision = "0007_integrated_heads"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    duplicate = bind.execute(
        sa.text(
            "SELECT feed_item_id, array_agg(cluster_id ORDER BY cluster_id) "
            "FROM cluster_items GROUP BY feed_item_id HAVING count(*) > 1 LIMIT 1"
        )
    ).first()
    if duplicate:
        raise RuntimeError(
            f"Cannot add unique cluster membership: feed_item_id={duplicate[0]} "
            f"belongs to clusters {duplicate[1]}"
        )
    invalid = bind.execute(
        sa.text(
            "SELECT c.id, count(ci.id) AS members, "
            "count(ci.id) FILTER (WHERE ci.is_primary) AS primaries "
            "FROM content_clusters c LEFT JOIN cluster_items ci ON ci.cluster_id = c.id "
            "GROUP BY c.id HAVING count(ci.id) < 2 "
            "OR count(ci.id) FILTER (WHERE ci.is_primary) <> 1 LIMIT 1"
        )
    ).first()
    if invalid:
        raise RuntimeError(
            f"Cannot add cluster constraints: cluster_id={invalid[0]} has "
            f"{invalid[1]} members and {invalid[2]} primaries"
        )
    op.create_index("uq_cluster_items_feed_item_id", "cluster_items", ["feed_item_id"], unique=True)
    op.create_index(
        "uq_cluster_items_primary_per_cluster",
        "cluster_items",
        ["cluster_id"],
        unique=True,
        postgresql_where=sa.text("is_primary = true"),
    )


def downgrade() -> None:
    op.drop_index("uq_cluster_items_primary_per_cluster", table_name="cluster_items")
    op.drop_index("uq_cluster_items_feed_item_id", table_name="cluster_items")
