"""Select an AI adapter independently for each operation."""

from app.ai.base import ImportanceAdapter, LLMAdapter, LLMError, RelevanceAdapter
from app.ai.jev import JevImportanceAdapter, JevRelevanceAdapter
from app.ai.openai_responses import (
    OpenAIImportanceAdapter,
    OpenAIRelevanceAdapter,
    OpenAISummaryAdapter,
)
from app.ai.openrouter import OpenRouterAdapter
from app.core.config import Settings


def create_summary_adapter(
    settings: Settings,
    *,
    model: str | None = None,
) -> LLMAdapter:
    provider = settings.llm_provider.strip().lower()
    if provider == "openrouter":
        return OpenRouterAdapter(settings, model=model)
    if provider == "openai":
        return OpenAISummaryAdapter(
            settings, model=model if model is not None else settings.openai_summary_model
        )
    raise LLMError("Для суммаризации настроен неподдерживаемый провайдер.")


def create_importance_adapter(
    settings: Settings,
    *,
    model: str | None = None,
) -> ImportanceAdapter:
    provider = settings.importance_provider.strip().lower()
    if provider == "typesafe":
        return JevImportanceAdapter(settings, model=model)
    if provider == "openai":
        return OpenAIImportanceAdapter(
            settings, model=model if model is not None else settings.openai_importance_model
        )
    raise LLMError("Для общей важности настроен неподдерживаемый провайдер.")


def create_relevance_adapter(
    settings: Settings,
    *,
    model: str | None = None,
) -> RelevanceAdapter:
    provider = settings.relevance_provider.strip().lower()
    if provider == "typesafe":
        return JevRelevanceAdapter(settings, model=model)
    if provider == "openai":
        return OpenAIRelevanceAdapter(
            settings, model=model if model is not None else settings.openai_relevance_model
        )
    raise LLMError("Для релевантности настроен неподдерживаемый провайдер.")
