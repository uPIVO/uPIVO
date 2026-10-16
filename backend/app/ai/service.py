import time
from collections.abc import Mapping
from typing import Any

from pydantic import ValidationError

from app.ai.base import LLMAdapter, LLMError
from app.ai.dto import ContentUnitInput, SummarizationRun, SummaryResult


class SummarizationService:
    def __init__(self, adapter: LLMAdapter) -> None:
        self._adapter = adapter

    async def summarize(
        self,
        unit: ContentUnitInput | Mapping[str, Any],
        *,
        prior_result: SummaryResult | Mapping[str, Any] | None = None,
        force: bool = False,
    ) -> SummarizationRun:
        started = time.perf_counter()
        try:
            content_unit = ContentUnitInput.model_validate(unit)
        except ValidationError:
            raise LLMError("Некорректные данные единицы контента.") from None

        previous: SummaryResult | None = None
        if prior_result is not None:
            try:
                previous = SummaryResult.model_validate(prior_result)
            except ValidationError:
                raise LLMError("Некорректный формат предыдущего результата.") from None

        try:
            fingerprint = self._adapter.fingerprint_for(content_unit)
        except Exception:
            raise LLMError("Не удалось проверить актуальность выжимки.") from None

        if previous is not None and not force and previous.fingerprint == fingerprint:
            return SummarizationRun(
                result=previous,
                reused=True,
                elapsed_seconds=time.perf_counter() - started,
            )

        try:
            generated = SummaryResult.model_validate(await self._adapter.summarize(content_unit))
        except LLMError:
            raise
        except Exception:
            raise LLMError("Не удалось получить выжимку.") from None

        if generated.fingerprint != fingerprint:
            raise LLMError("Адаптер вернул выжимку с несовпадающей версией входных данных.")

        return SummarizationRun(
            result=generated,
            reused=False,
            elapsed_seconds=time.perf_counter() - started,
        )
