from typing import Protocol

from app.ai.dto import ContentUnitInput, SummaryResult


class LLMError(RuntimeError):
    """Ошибка суммаризации без данных провайдера или исходного текста."""


class LLMAdapter(Protocol):
    def fingerprint_for(self, unit: ContentUnitInput) -> str: ...

    async def summarize(self, unit: ContentUnitInput) -> SummaryResult: ...
