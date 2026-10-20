from __future__ import annotations

import hashlib
import json
import math
from typing import Any

import httpx

import app.ai.importance as importance_config
from app.ai.analysis_policy import (
    AnalysisPolicy,
    load_analysis_policy,
    validate_analysis_policy,
)
from app.ai.base import LLMError
from app.ai.dto import (
    ImportanceComponents,
    ImportanceInput,
    ImportanceUsage,
    RelevanceInput,
    RelevanceResult,
    RelevanceUsage,
)
from app.core.config import Settings

_PROVIDER = "typesafe"


def _validated_typesafe_api_key(api_key: Any) -> str:
    value = api_key.get_secret_value().strip()
    if (
        not value
        or not value.isascii()
        or any(not character.isprintable() or character.isspace() for character in value)
    ):
        raise LLMError("Не задан или некорректен API-ключ TypeSafe.")
    return value


class JevRelevanceAdapter:
    def __init__(
        self,
        settings: Settings,
        *,
        model: str | None = None,
        policy: AnalysisPolicy | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        base_url = settings.typesafe_base_url.strip()
        try:
            parsed_url = httpx.URL(base_url)
        except httpx.InvalidURL:
            raise LLMError("Некорректный адрес TypeSafe.") from None
        if (
            parsed_url.scheme != "https"
            or not parsed_url.host
            or parsed_url.username
            or parsed_url.password
            or parsed_url.query
            or parsed_url.fragment
        ):
            raise LLMError("Некорректный адрес TypeSafe.")

        configured_model = settings.typesafe_model if model is None else model
        if not isinstance(configured_model, str) or not configured_model.strip():
            raise LLMError("Не задана или некорректна модель TypeSafe.")
        selected_policy = load_analysis_policy() if policy is None else policy
        self.policy = validate_analysis_policy(selected_policy)

        self.provider = _PROVIDER
        self.base_url = str(parsed_url).rstrip("/")
        self.model = configured_model.strip()
        self.timeout_seconds = float(settings.typesafe_timeout_seconds)
        self._api_key = settings.typesafe_api_key
        self._client = client
        self._owns_client = False

    async def __aenter__(self) -> JevRelevanceAdapter:
        if self._client is None:
            self._client = self._new_client()
            self._owns_client = True
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None
            self._owns_client = False

    def fingerprint_for(self, value: RelevanceInput) -> str:
        material = {
            "fingerprint_version": 1,
            "provider": self.provider,
            "base_url": self.base_url,
            "model": self.model,
            "prompt_version": self.policy.relevance.prompt_version,
            "instructions": self.policy.relevance.instructions,
            "question": {"id": self.policy.relevance.question_id, "type": "noul"},
            "summary": value.summary,
            "interests": value.interests,
        }
        encoded = json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return f"v1:{hashlib.sha256(encoded.encode('utf-8')).hexdigest()}"

    def validate_configuration(self) -> None:
        _validated_typesafe_api_key(self._api_key)

    async def score_relevance(self, value: RelevanceInput) -> RelevanceResult:
        api_key = _validated_typesafe_api_key(self._api_key)
        if self._client is not None:
            return await self._request_relevance(self._client, value, api_key)

        async with self._new_client() as client:
            return await self._request_relevance(client, value, api_key)

    def _new_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=self.timeout_seconds)

    async def _request_relevance(
        self,
        client: httpx.AsyncClient,
        value: RelevanceInput,
        api_key: str,
    ) -> RelevanceResult:
        request_body = {
            "state": {"summary": value.summary, "interests": value.interests},
            "model": self.model,
            "questions": {
                self.policy.relevance.question_id: {
                    "type": "noul",
                    "instructions": self.policy.relevance.instructions,
                }
            },
        }
        try:
            response = await client.post(
                f"{self.base_url}/v1/systemone",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=request_body,
                timeout=self.timeout_seconds,
            )
        except httpx.TimeoutException:
            raise LLMError("Истекло время ожидания ответа TypeSafe.") from None
        except httpx.RequestError:
            raise LLMError("Не удалось подключиться к TypeSafe.") from None

        if response.status_code == 429:
            raise LLMError("TypeSafe ограничил частоту запросов (HTTP 429).")
        if response.status_code == 529:
            raise LLMError("TypeSafe временно перегружен (HTTP 529).")
        if response.status_code < 200 or response.status_code >= 300:
            raise LLMError(f"TypeSafe вернул ошибку HTTP {response.status_code}.")

        try:
            payload = response.json()
        except ValueError:
            raise LLMError("TypeSafe вернул некорректный JSON-ответ.") from None

        model, relevance = self._read_noul(payload)
        usage = self._read_usage(payload)
        return RelevanceResult(
            relevance=relevance,
            fingerprint=self.fingerprint_for(value),
            provider=self.provider,
            model=model,
            usage=usage,
        )

    def _read_noul(self, payload: Any) -> tuple[str, float]:
        if not isinstance(payload, dict) or payload.get("error") is not None:
            raise LLMError("TypeSafe вернул некорректный ответ о релевантности.")

        model = payload.get("model")
        answers = payload.get("answers")
        if not isinstance(model, str) or not model.strip() or not isinstance(answers, dict):
            raise LLMError("TypeSafe вернул некорректный ответ о релевантности.")

        answer = answers.get(self.policy.relevance.question_id)
        if not isinstance(answer, dict) or answer.get("type") != "noul":
            raise LLMError("TypeSafe вернул некорректный ответ о релевантности.")

        value = answer.get("noul")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise LLMError("TypeSafe вернул некорректный ответ о релевантности.")
        try:
            probability = float(value)
        except (OverflowError, ValueError):
            raise LLMError("TypeSafe вернул некорректный ответ о релевантности.") from None
        if not math.isfinite(probability) or not 0.0 <= probability <= 1.0:
            raise LLMError("TypeSafe вернул некорректный ответ о релевантности.")
        return model.strip(), probability

    @staticmethod
    def _read_usage(payload: Any) -> RelevanceUsage | None:
        if not isinstance(payload, dict):
            return None
        raw_usage = payload.get("usage")
        if not isinstance(raw_usage, dict):
            return None

        token_values: dict[str, int] = {}
        for field in ("input_tokens", "output_tokens"):
            value = raw_usage.get(field)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                token_values[field] = value
        return RelevanceUsage(**token_values) if token_values else None


class JevImportanceAdapter:
    def __init__(
        self,
        settings: Settings,
        *,
        model: str | None = None,
        policy: AnalysisPolicy | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        base_url = settings.typesafe_base_url.strip()
        try:
            parsed_url = httpx.URL(base_url)
        except httpx.InvalidURL:
            raise LLMError("Некорректный адрес TypeSafe.") from None
        if (
            parsed_url.scheme != "https"
            or not parsed_url.host
            or parsed_url.username
            or parsed_url.password
            or parsed_url.query
            or parsed_url.fragment
        ):
            raise LLMError("Некорректный адрес TypeSafe.")

        configured_model = settings.typesafe_model if model is None else model
        if not isinstance(configured_model, str) or not configured_model.strip():
            raise LLMError("Не задана или некорректна модель TypeSafe.")
        selected_policy = load_analysis_policy() if policy is None else policy
        self.policy = validate_analysis_policy(selected_policy)

        self.provider = _PROVIDER
        self.base_url = str(parsed_url).rstrip("/")
        self.model = configured_model.strip()
        self.timeout_seconds = float(settings.typesafe_timeout_seconds)
        self._api_key = settings.typesafe_api_key
        self._client = client
        self._owns_client = False

    async def __aenter__(self) -> JevImportanceAdapter:
        if self._client is None:
            self._client = self._new_client()
            self._owns_client = True
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None
            self._owns_client = False

    def fingerprint_for(self, value: ImportanceInput) -> str:
        material = {
            "fingerprint_version": 1,
            "provider": self.provider,
            "base_url": self.base_url,
            "model": self.model,
            "prompt_version": self.policy.importance.prompt_version,
            "questions": [
                {"id": question_id, **question}
                for question_id, question in self._question_entries()
            ],
            "aggregation_version": importance_config.IMPORTANCE_AGGREGATION_VERSION,
            "weights": importance_config.IMPORTANCE_WEIGHTS.model_dump(mode="json"),
            "summary": value.summary,
        }
        encoded = json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return f"v1:{hashlib.sha256(encoded.encode('utf-8')).hexdigest()}"

    def validate_configuration(self) -> None:
        _validated_typesafe_api_key(self._api_key)

    async def score_importance(self, value: ImportanceInput) -> ImportanceComponents:
        api_key = _validated_typesafe_api_key(self._api_key)
        if self._client is not None:
            return await self._request_importance(self._client, value, api_key)

        async with self._new_client() as client:
            return await self._request_importance(client, value, api_key)

    def _new_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=self.timeout_seconds)

    def _question_entries(self) -> list[tuple[str, dict[str, Any]]]:
        return [
            (
                question.question_id,
                {
                    "type": "score",
                    "instructions": "\n\n".join(
                        (
                            self.policy.importance.instructions,
                            question.prompt,
                            question.additional_instructions,
                        )
                    ),
                    "criteria": list(question.criteria),
                },
            )
            for question in self.policy.importance.questions
        ]

    def _questions(self) -> dict[str, dict[str, Any]]:
        return dict(self._question_entries())

    async def _request_importance(
        self,
        client: httpx.AsyncClient,
        value: ImportanceInput,
        api_key: str,
    ) -> ImportanceComponents:
        request_body = {
            "state": value.summary,
            "model": self.model,
            "questions": self._questions(),
        }
        try:
            response = await client.post(
                f"{self.base_url}/v1/systemone",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=request_body,
                timeout=self.timeout_seconds,
            )
        except httpx.TimeoutException:
            raise LLMError("Истекло время ожидания ответа TypeSafe.") from None
        except httpx.RequestError:
            raise LLMError("Не удалось подключиться к TypeSafe.") from None

        if response.status_code == 429:
            raise LLMError("TypeSafe ограничил частоту запросов (HTTP 429).")
        if response.status_code == 529:
            raise LLMError("TypeSafe временно перегружен (HTTP 529).")
        if response.status_code < 200 or response.status_code >= 300:
            raise LLMError(f"TypeSafe вернул ошибку HTTP {response.status_code}.")

        try:
            payload = response.json()
        except ValueError:
            raise LLMError("TypeSafe вернул некорректный JSON-ответ.") from None

        model, scores = self._read_scores(payload)
        usage = self._read_importance_usage(payload)
        return ImportanceComponents(
            scale_score=scores["scale"],
            significance_score=scores["significance"],
            reaction_score=scores["reaction"],
            fingerprint=self.fingerprint_for(value),
            provider=self.provider,
            model=model,
            usage=usage,
        )

    def _read_scores(self, payload: Any) -> tuple[str, dict[str, float]]:
        if not isinstance(payload, dict) or payload.get("error") is not None:
            raise LLMError("TypeSafe вернул некорректный ответ об общей важности.")

        model = payload.get("model")
        answers = payload.get("answers")
        if not isinstance(model, str) or not model.strip() or not isinstance(answers, dict):
            raise LLMError("TypeSafe вернул некорректный ответ об общей важности.")

        scores: dict[str, float] = {}
        for question in self.policy.importance.questions:
            question_id = question.question_id
            answer = answers.get(question_id)
            if not isinstance(answer, dict) or answer.get("type") != "score":
                raise LLMError("TypeSafe вернул некорректный ответ об общей важности.")

            raw_score = answer.get("score")
            if isinstance(raw_score, bool) or not isinstance(raw_score, (int, float)):
                raise LLMError("TypeSafe вернул некорректный ответ об общей важности.")
            try:
                score = float(raw_score)
            except (OverflowError, ValueError):
                raise LLMError("TypeSafe вернул некорректный ответ об общей важности.") from None
            if not math.isfinite(score) or not 0.0 <= score <= len(question.criteria) - 1:
                raise LLMError("TypeSafe вернул некорректный ответ об общей важности.")
            scores[question_id] = score

        return model.strip(), scores

    @staticmethod
    def _read_importance_usage(payload: Any) -> ImportanceUsage | None:
        if not isinstance(payload, dict):
            return None
        raw_usage = payload.get("usage")
        if not isinstance(raw_usage, dict):
            return None

        token_values: dict[str, int] = {}
        for field in ("input_tokens", "output_tokens"):
            value = raw_usage.get(field)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                token_values[field] = value
        return ImportanceUsage(**token_values) if token_values else None
