"""One run of the publication pipeline: collect, normalize, cluster.

Calls the existing collectors, normalizer, and clusterer. It does not
reimplement fetch, payload mapping, or story matching.

CLI from backend/ (PostgreSQL up, migrations applied, user already has sources):

    uv run python -m app.pipeline --user demo@example.com
    uv run python -m app.pipeline --user 1 --dry-run
    uv run python -m app.pipeline --fixture tests/fixtures/pipeline/sample.json

Re-running is safe: collectors upsert the same external_id, normalization
only fills pending rows, clustering updates the same content_clusters rows.
"""

from app.pipeline.run import (
    FixturePipelineResult,
    PipelineResult,
    chain_payloads,
    run_pipeline,
)
from app.services.collect import UnknownUserError

__all__ = [
    "FixturePipelineResult",
    "PipelineResult",
    "UnknownUserError",
    "chain_payloads",
    "run_pipeline",
]
