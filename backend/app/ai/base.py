from typing import Protocol

from app.ai.dto import (
    ContentUnitInput,
    ImportanceComponents,
    ImportanceInput,
    RelevanceInput,
    RelevanceResult,
    SummaryResult,
)


class LLMError(RuntimeError):
    """Ошибка AI-операции без данных провайдера или исходного текста."""


class LLMAdapter(Protocol):
    def validate_configuration(self) -> None: ...

    def fingerprint_for(self, unit: ContentUnitInput) -> str: ...

    async def summarize(self, unit: ContentUnitInput) -> SummaryResult: ...


class RelevanceAdapter(Protocol):
    def validate_configuration(self) -> None: ...

    def fingerprint_for(self, value: RelevanceInput) -> str: ...

    async def score_relevance(self, value: RelevanceInput) -> RelevanceResult: ...


class ImportanceAdapter(Protocol):
    def validate_configuration(self) -> None: ...

    def fingerprint_for(self, value: ImportanceInput) -> str: ...

    async def score_importance(self, value: ImportanceInput) -> ImportanceComponents: ...
