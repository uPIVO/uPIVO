from typing import Protocol


class LLMAdapter(Protocol):
    """Provider-neutral contract; no provider or API calls implemented."""

    async def summarize(self, text: str) -> str: ...

    async def score_relevance(self, text: str, topic: str) -> float: ...
