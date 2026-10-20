"""Story clustering: exact content_hash duplicates and similar title + body.

Publications stay in feed_items. A story with two or more items becomes one
content_clusters row and cluster_items links. One of them is primary.

CLI from backend/:

    uv run python -m app.clustering --fixture tests/fixtures/cluster/sample.json
    uv run python -m app.clustering
"""

from app.clustering.cluster import StoryItem, build_plans
from app.clustering.persist import cluster_feed

__all__ = [
    "StoryItem",
    "build_plans",
    "cluster_feed",
]
