"""Stateless OpenAI Responses API adapters for the three AI operations."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from typing import Any

import httpx
from pydantic import ValidationError

import app.ai.importance as importance_config
from app.ai.analysis_policy import AnalysisPolicy, load_analysis_policy, validate_analysis_policy
from app.ai.base import LLMError
from app.ai.dto import (
    ContentUnitInput,
    ImportanceComponents,
    ImportanceInput,
    ImportanceUsage,
    RelevanceInput,
    RelevanceResult,
    RelevanceUsage,
    SummaryResult,
    SummaryUsage,
)
from app.core.config import Settings


def _fingerprint(material: dict[str, Any]) -> str:
    encoded = json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"v1:{hashlib.sha256(encoded.encode('utf-8')).hexdigest()}"


def _usage(payload: dict[str, Any]) -> dict[str, int]:
    raw = payload.get("usage")
    if not isinstance(raw, dict):
        return {}
    return {
        key: value
        for key in ("input_tokens", "output_tokens", "total_tokens")
        if isinstance((value := raw.get(key)), int) and not isinstance(value, bool) and value >= 0
    }


class _OpenAIResponsesAdapter:
    provider = "openai"

    def __init__(
        self,
        settings: Settings,
        *,
        model: str | None = None,
        policy: AnalysisPolicy | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        try:
            url = httpx.URL(settings.openai_base_url.strip())
        except httpx.InvalidURL:
            raise LLMError("Некорректный адрес OpenAI.") from None
        if (
            url.scheme != "https"
            or not url.host
            or url.username
            or url.password
            or url.query
            or url.fragment
        ):
            raise LLMError("Некорректный адрес OpenAI.")
        configured_model = settings.openai_model if model is None else model
        if not isinstance(configured_model, str) or not configured_model.strip():
            raise LLMError("Не задана модель OpenAI.")
        self.base_url = str(url).rstrip("/")
        self.model = configured_model.strip()
        self.timeout_seconds = float(settings.openai_timeout_seconds)
        self.policy = validate_analysis_policy(load_analysis_policy() if policy is None else policy)
        self._api_key = settings.openai_api_key
        self._client = client

    def validate_configuration(self) -> None:
        key = self._api_key.get_secret_value().strip()
        if (
            not key
            or not key.isascii()
            or any(not char.isprintable() or char.isspace() for char in key)
        ):
            raise LLMError("Не задан или некорректен API-ключ OpenAI.")

    def _material(self, prompt_version: str, policy: Any, value: Any) -> dict[str, Any]:
        return {
            "fingerprint_version": 1,
            "provider": self.provider,
            "base_url": self.base_url,
            "model": self.model,
            "prompt_version": prompt_version,
            "policy": asdict(policy),
            "input": value,
        }

    async def _response(
        self, system: str, user: str, *, format_schema: dict[str, Any] | None = None
    ) -> tuple[str, dict[str, int]]:
        self.validate_configuration()
        body: dict[str, Any] = {
            "model": self.model,
            "store": False,
            "input": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        if format_schema is not None:
            body["text"] = {"format": format_schema}

        async def send(client: httpx.AsyncClient) -> httpx.Response:
            return await client.post(
                f"{self.base_url}/responses",
                headers={
                    "Authorization": f"Bearer {self._api_key.get_secret_value().strip()}",
                    "Content-Type": "application/json",
                },
                json=body,
                timeout=self.timeout_seconds,
                follow_redirects=False,
            )

        try:
            if self._client is None:
                async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                    response = await send(client)
            else:
                response = await send(self._client)
        except httpx.TimeoutException:
            raise LLMError("Истекло время ожидания ответа OpenAI.") from None
        except httpx.RequestError:
            raise LLMError("Не удалось подключиться к OpenAI.") from None

        if response.status_code == 429:
            raise LLMError("OpenAI ограничил частоту запросов (HTTP 429).")
        if not 200 <= response.status_code < 300:
            raise LLMError(f"OpenAI вернул ошибку HTTP {response.status_code}.")
        try:
            payload = response.json()
        except ValueError:
            raise LLMError("OpenAI вернул некорректный JSON-ответ.") from None
        if not isinstance(payload, dict):
            raise LLMError("OpenAI вернул некорректный ответ.")
        if payload.get("error") is not None:
            raise LLMError("OpenAI вернул ответ с ошибкой.")
        if payload.get("status") != "completed":
            raise LLMError("OpenAI не завершил генерацию ответа.")
        output = payload.get("output")
        if not isinstance(output, list):
            raise LLMError("OpenAI не вернул текст ответа.")
        texts: list[str] = []
        for item in output:
            if not isinstance(item, dict) or item.get("type") != "message":
                continue
            if item.get("status") not in (None, "completed"):
                raise LLMError("OpenAI не завершил генерацию ответа.")
            content = item.get("content")
            if not isinstance(content, list):
                raise LLMError("OpenAI вернул некорректный ответ.")
            for part in content:
                if not isinstance(part, dict):
                    raise LLMError("OpenAI вернул некорректный ответ.")
                if part.get("type") == "refusal":
                    raise LLMError("OpenAI отказался выполнить запрос.")
                if part.get("type") == "output_text":
                    if not isinstance(part.get("text"), str):
                        raise LLMError("OpenAI вернул некорректный ответ.")
                    texts.append(part["text"])
        result = "".join(texts).strip()
        if not result:
            raise LLMError("OpenAI вернул пустой ответ.")
        return result, _usage(payload)


def _score_schema(name: str, properties: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return {
        "type": "json_schema",
        "name": name,
        "strict": True,
        "schema": {
            "type": "object",
            "properties": properties,
            "required": list(properties),
            "additionalProperties": False,
        },
    }


class OpenAISummaryAdapter(_OpenAIResponsesAdapter):
    def fingerprint_for(self, unit: ContentUnitInput) -> str:
        return _fingerprint(
            self._material(
                self.policy.summary.prompt_version,
                self.policy.summary,
                unit.model_dump(mode="json", exclude_none=False),
            )
        )

    async def summarize(self, unit: ContentUnitInput) -> SummaryResult:
        try:
            serialized = json.dumps(
                unit.model_dump(mode="json", exclude_none=False),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        except (TypeError, ValueError):
            raise LLMError("Не удалось сериализовать данные единицы контента.") from None
        summary, tokens = await self._response(
            self.policy.summary.system_prompt,
            self.policy.summary.user_prompt_prefix + serialized,
        )
        return SummaryResult(
            summary=summary,
            fingerprint=self.fingerprint_for(unit),
            provider=self.provider,
            preset=self.model,
            usage=SummaryUsage(
                prompt_tokens=tokens.get("input_tokens"),
                completion_tokens=tokens.get("output_tokens"),
                total_tokens=tokens.get("total_tokens"),
            )
            if tokens
            else None,
            cost_usd=None,
        )


class OpenAIImportanceAdapter(_OpenAIResponsesAdapter):
    def fingerprint_for(self, value: ImportanceInput) -> str:
        material = self._material(
            self.policy.importance.prompt_version, self.policy.importance, value.model_dump()
        )
        material["aggregation_version"] = importance_config.IMPORTANCE_AGGREGATION_VERSION
        material["weights"] = importance_config.IMPORTANCE_WEIGHTS.model_dump(mode="json")
        return _fingerprint(material)

    async def score_importance(self, value: ImportanceInput) -> ImportanceComponents:
        criteria = "\n\n".join(
            f"{question.question_id}_score: {question.prompt}\n"
            f"{question.additional_instructions}\n"
            + "\n".join(
                f"{index}: {criterion}" for index, criterion in enumerate(question.criteria)
            )
            for question in self.policy.importance.questions
        )
        schema = _score_schema(
            "importance_scores",
            {
                "scale_score": {"type": "integer", "minimum": 0, "maximum": 4},
                "significance_score": {"type": "integer", "minimum": 0, "maximum": 4},
                "reaction_score": {"type": "integer", "minimum": 0, "maximum": 2},
            },
        )
        raw, tokens = await self._response(
            self.policy.importance.instructions + "\n\n" + criteria,
            json.dumps(value.model_dump(), ensure_ascii=False),
            format_schema=schema,
        )
        try:
            scores = json.loads(raw)
            if not isinstance(scores, dict) or set(scores) != set(schema["schema"]["properties"]):
                raise ValueError
            if any(type(score) is not int for score in scores.values()):
                raise ValueError
            return ImportanceComponents(
                **scores,
                fingerprint=self.fingerprint_for(value),
                provider=self.provider,
                model=self.model,
                usage=(
                    ImportanceUsage(
                        **{
                            key: tokens[key]
                            for key in ("input_tokens", "output_tokens")
                            if key in tokens
                        }
                    )
                    if tokens
                    else None
                ),
            )
        except (ValueError, TypeError, ValidationError):
            raise LLMError("OpenAI вернул некорректную оценку общей важности.") from None


class OpenAIRelevanceAdapter(_OpenAIResponsesAdapter):
    def fingerprint_for(self, value: RelevanceInput) -> str:
        return _fingerprint(
            self._material(
                self.policy.relevance.prompt_version,
                self.policy.relevance,
                value.model_dump(),
            )
        )

    async def score_relevance(self, value: RelevanceInput) -> RelevanceResult:
        schema = _score_schema(
            "relevance_score", {"relevance": {"type": "number", "minimum": 0, "maximum": 1}}
        )
        raw, tokens = await self._response(
            self.policy.relevance.instructions,
            json.dumps(value.model_dump(), ensure_ascii=False),
            format_schema=schema,
        )
        try:
            result = json.loads(raw)
            if not isinstance(result, dict) or set(result) != {"relevance"}:
                raise ValueError
            return RelevanceResult(
                relevance=result["relevance"],
                fingerprint=self.fingerprint_for(value),
                provider=self.provider,
                model=self.model,
                usage=(
                    RelevanceUsage(
                        **{
                            key: tokens[key]
                            for key in ("input_tokens", "output_tokens")
                            if key in tokens
                        }
                    )
                    if tokens
                    else None
                ),
            )
        except (ValueError, TypeError, ValidationError):
            raise LLMError("OpenAI вернул некорректную оценку релевантности.") from None
