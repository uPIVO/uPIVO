"""Join independent migrations for AI analysis, clustering, folders, and digests.

Revision ID: 0007_integrated_heads
Revises: 0006_content_clusters, 0006_feed_item_ai_analysis, 0006_folders, 01add98b80ff
"""

revision = "0007_integrated_heads"
down_revision = (
    "0006_content_clusters",
    "0006_feed_item_ai_analysis",
    "0006_folders",
    "01add98b80ff",
)
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
