from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any

from pydantic import ValidationError

from app.ai.base import ImportanceAdapter, LLMError
from app.ai.dto import (
    ImportanceComponents,
    ImportanceInput,
    ImportanceResult,
    ImportanceRun,
    ImportanceWeights,
)

IMPORTANCE_AGGREGATION_VERSION = "content-importance-sum-v2"
IMPORTANCE_WEIGHTS = ImportanceWeights(scale=1.0, significance=1.0, reaction=1.0)


class ImportanceService:
    def __init__(self, adapter: ImportanceAdapter) -> None:
        self._adapter = adapter

    def validate_configuration(self) -> None:
        self._adapter.validate_configuration()

    async def score_importance(
        self,
        value: ImportanceInput | Mapping[str, Any],
        *,
        prior_result: ImportanceResult | Mapping[str, Any] | None = None,
        force: bool = False,
    ) -> ImportanceRun:
        started = time.perf_counter()
        try:
            value_payload = value.model_dump() if isinstance(value, ImportanceInput) else value
            importance_input = ImportanceInput.model_validate(value_payload)
        except ValidationError:
            raise LLMError("Некорректные данные для оценки общей важности.") from None

        previous: ImportanceResult | None = None
        if prior_result is not None:
            try:
                previous = ImportanceResult.model_validate(prior_result)
            except ValidationError:
                raise LLMError("Некорректный формат предыдущей оценки общей важности.") from None

        try:
            fingerprint = self._adapter.fingerprint_for(importance_input)
        except Exception:
            raise LLMError("Не удалось проверить актуальность оценки общей важности.") from None

        if (
            previous is not None
            and not force
            and previous.fingerprint == fingerprint
            and previous.aggregation_version == IMPORTANCE_AGGREGATION_VERSION
            and previous.weights == IMPORTANCE_WEIGHTS
        ):
            return ImportanceRun(
                result=previous,
                reused=True,
                elapsed_seconds=time.perf_counter() - started,
            )

        try:
            generated = ImportanceComponents.model_validate(
                await self._adapter.score_importance(importance_input)
            )
            total_score = (
                IMPORTANCE_WEIGHTS.scale * generated.scale_score
                + IMPORTANCE_WEIGHTS.significance * generated.significance_score
                + IMPORTANCE_WEIGHTS.reaction * generated.reaction_score
            )
            result = ImportanceResult(
                **generated.model_dump(),
                total_score=total_score,
                weights=IMPORTANCE_WEIGHTS,
                aggregation_version=IMPORTANCE_AGGREGATION_VERSION,
            )
        except LLMError:
            raise
        except Exception:
            raise LLMError("Не удалось получить оценку общей важности.") from None

        if result.fingerprint != fingerprint:
            raise LLMError("Адаптер вернул оценку с несовпадающей версией входных данных.")

        return ImportanceRun(
            result=result,
            reused=False,
            elapsed_seconds=time.perf_counter() - started,
        )
