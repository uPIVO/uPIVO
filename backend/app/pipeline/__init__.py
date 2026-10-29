"""One run of the publication pipeline: collect, normalize, cluster, analyze.

Calls the existing collectors, normalizer, clusterer and the common AI stage.
It does not reimplement fetch, payload mapping, story matching, prompts,
fingerprints or AI storage.

CLI from backend/ (PostgreSQL up, migrations applied, user already has sources):

    uv run python -m app.pipeline --user demo@example.com
    uv run python -m app.pipeline --user 1 --dry-run
    uv run python -m app.pipeline --fixture tests/fixtures/pipeline/sample.json

Re-running is safe: collectors upsert the same external_id, normalization
only fills pending rows, clustering updates the same content_clusters rows,
and the AI stage reuses current results.
"""

from app.pipeline.analysis import AnalysisStats, run_common_analysis
from app.pipeline.run import (
    FixturePipelineResult,
    PipelineResult,
    chain_payloads,
    run_pipeline,
)
from app.services.collect import UnknownUserError

__all__ = [
    "AnalysisStats",
    "FixturePipelineResult",
    "PipelineResult",
    "UnknownUserError",
    "chain_payloads",
    "run_common_analysis",
    "run_pipeline",
]
