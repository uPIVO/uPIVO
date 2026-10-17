from typing import Protocol

from app.ai.dto import ContentUnitInput, RelevanceInput, RelevanceResult, SummaryResult


class LLMError(RuntimeError):
    """Ошибка AI-операции без данных провайдера или исходного текста."""


class LLMAdapter(Protocol):
    def fingerprint_for(self, unit: ContentUnitInput) -> str: ...

    async def summarize(self, unit: ContentUnitInput) -> SummaryResult: ...


class RelevanceAdapter(Protocol):
    def fingerprint_for(self, value: RelevanceInput) -> str: ...

    async def score_relevance(self, value: RelevanceInput) -> RelevanceResult: ...
