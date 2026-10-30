from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from app.ai.analysis_policy import load_analysis_policy
from app.ai.base import LLMError
from app.ai.dto import ContentUnitInput, ImportanceInput, RelevanceInput
from app.ai.factory import (
    create_importance_adapter,
    create_relevance_adapter,
    create_summary_adapter,
)
from app.ai.importance import ImportanceService
from app.ai.jev import JevImportanceAdapter, JevRelevanceAdapter
from app.ai.openai_responses import (
    OpenAIImportanceAdapter,
    OpenAIRelevanceAdapter,
    OpenAISummaryAdapter,
)
from app.ai.openrouter import OpenRouterAdapter
from app.core.config import Settings

KEY = "test-openai-key"
SECRET_BODY = "Секретное содержимое публикации"
INTERESTS = ["космос", "медицина", "история"]


def settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "_env_file": None,
        "openai_model": "gpt-5.4-mini",
        "openai_base_url": "https://api.openai.com/v1",
        "llm_provider": "openai",
        "importance_provider": "openai",
        "relevance_provider": "openai",
        "openai_api_key": SecretStr(KEY),
        "llm_api_key": SecretStr(""),
        "typesafe_api_key": SecretStr(""),
    }
    values.update(overrides)
    return Settings(**values)


def unit() -> ContentUnitInput:
    return ContentUnitInput.model_validate(
        {
            "kind": "feed_item",
            "id": 1,
            "feed_items": [
                {
                    "feed_item_id": 1,
                    "body_text": SECRET_BODY,
                    "original_url": "https://example.test/1",
                }
            ],
        }
    )


def payload(text: str, *, status: str = "completed", usage: Any = None) -> dict[str, Any]:
    return {
        "status": status,
        "output": [
            {
                "type": "message",
                "status": "completed",
                "content": [{"type": "output_text", "text": text}],
            }
        ],
        "usage": usage,
    }


def run(adapter_type: type, value: Any, handler: Any, **kwargs: Any) -> Any:
    async def exercise() -> Any:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            adapter = adapter_type(settings(**kwargs), client=client)
            if adapter_type is OpenAISummaryAdapter:
                return await adapter.summarize(value)
            if adapter_type is OpenAIImportanceAdapter:
                return await adapter.score_importance(value)
            return await adapter.score_relevance(value)

    return asyncio.run(exercise())


def test_summary_request_policy_usage_and_fingerprint() -> None:
    policy = load_analysis_policy()

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert str(request.url) == "https://api.openai.com/v1/responses"
        assert request.headers["Authorization"] == f"Bearer {KEY}"
        assert request.headers["Content-Type"] == "application/json"
        assert body["model"] == "gpt-5.4-mini"
        assert body["store"] is False
        assert body["input"][0] == {"role": "system", "content": policy.summary.system_prompt}
        assert body["input"][1]["content"].startswith(policy.summary.user_prompt_prefix)
        assert SECRET_BODY in body["input"][1]["content"]
        assert "text" not in body
        return httpx.Response(
            200,
            json=payload(
                "Краткая выжимка",
                usage={
                    "input_tokens": 14,
                    "output_tokens": 8,
                    "total_tokens": 22,
                },
            ),
        )

    result = run(OpenAISummaryAdapter, unit(), handler)
    assert result.summary == "Краткая выжимка"
    assert result.provider == "openai"
    assert result.preset == "gpt-5.4-mini"
    assert result.usage.prompt_tokens == 14
    assert result.usage.completion_tokens == 8
    assert result.usage.total_tokens == 22
    assert result.cost_usd is None

    initial = OpenAISummaryAdapter(settings()).fingerprint_for(unit())
    assert initial == OpenAISummaryAdapter(
        settings(openai_api_key=SecretStr("another-key"))
    ).fingerprint_for(unit())
    assert initial != OpenAISummaryAdapter(settings(), model="other-model").fingerprint_for(unit())
    assert initial != OpenAISummaryAdapter(
        settings(openai_base_url="https://other.example/v1")
    ).fingerprint_for(unit())
    changed = unit().model_copy(
        update={
            "feed_items": [
                unit().feed_items[0].model_copy(update={"body_text": "Другое содержание"})
            ]
        }
    )
    assert initial != OpenAISummaryAdapter(settings()).fingerprint_for(changed)
    changed_policy = replace(
        policy, summary=replace(policy.summary, system_prompt="Другая политика")
    )
    assert initial != OpenAISummaryAdapter(settings(), policy=changed_policy).fingerprint_for(
        unit()
    )
    assert initial != OpenRouterAdapter(Settings(llm_provider="openrouter")).fingerprint_for(unit())


def test_importance_structured_schema_and_service() -> None:
    policy = load_analysis_policy()

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        schema = body["text"]["format"]
        assert schema["type"] == "json_schema" and schema["strict"] is True
        assert schema["schema"]["properties"]["scale_score"]["maximum"] == 4
        assert schema["schema"]["properties"]["reaction_score"]["maximum"] == 2
        assert "событие" in body["input"][0]["content"]
        assert policy.importance.instructions in body["input"][0]["content"]
        for question in policy.importance.questions:
            assert question.prompt in body["input"][0]["content"]
            assert question.criteria[0] in body["input"][0]["content"]
        assert body["store"] is False
        return httpx.Response(
            200,
            json=payload(
                '{"scale_score": 3, "significance_score": 4, "reaction_score": 2}',
                usage={"input_tokens": 20, "output_tokens": 9, "total_tokens": 29},
            ),
        )

    async def exercise() -> Any:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            adapter = OpenAIImportanceAdapter(settings(), client=client)
            return await ImportanceService(adapter).score_importance(
                ImportanceInput(summary="Событие")
            )

    result = asyncio.run(exercise()).result
    assert result.total_score == 9
    assert result.provider == "openai"
    assert result.usage.input_tokens == 20
    assert result.usage.output_tokens == 9
    value = ImportanceInput(summary="Событие")
    initial = OpenAIImportanceAdapter(settings()).fingerprint_for(value)
    assert initial == OpenAIImportanceAdapter(
        settings(openai_api_key=SecretStr("another-key"))
    ).fingerprint_for(value)
    assert initial != OpenAIImportanceAdapter(settings(), model="other").fingerprint_for(value)
    assert initial != OpenAIImportanceAdapter(
        settings(openai_base_url="https://other.example/v1")
    ).fingerprint_for(value)
    assert initial != OpenAIImportanceAdapter(settings()).fingerprint_for(
        ImportanceInput(summary="Другое событие")
    )
    policy = load_analysis_policy()
    changed = replace(policy, importance=replace(policy.importance, instructions="Изменено"))
    assert initial != OpenAIImportanceAdapter(settings(), policy=changed).fingerprint_for(value)


def test_relevance_structured_schema_full_interests_and_fingerprint() -> None:
    value = RelevanceInput(summary="Сводка", interests=INTERESTS)

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        schema = body["text"]["format"]
        assert schema["type"] == "json_schema" and schema["strict"] is True
        assert schema["schema"]["properties"]["relevance"]["maximum"] == 1
        assert json.loads(body["input"][1]["content"])["interests"] == INTERESTS
        return httpx.Response(
            200,
            json=payload(
                '{"relevance": 0.75}',
                usage={"input_tokens": 8, "output_tokens": 2},
            ),
        )

    result = run(OpenAIRelevanceAdapter, value, handler)
    assert result.relevance == 0.75
    assert result.provider == "openai"
    assert result.usage.input_tokens == 8
    initial = OpenAIRelevanceAdapter(settings()).fingerprint_for(value)
    assert initial == OpenAIRelevanceAdapter(
        settings(openai_api_key=SecretStr("another-key"))
    ).fingerprint_for(value)
    assert initial != OpenAIRelevanceAdapter(settings(), model="other").fingerprint_for(value)
    assert initial != OpenAIRelevanceAdapter(
        settings(openai_base_url="https://other.example/v1")
    ).fingerprint_for(value)
    assert initial != OpenAIRelevanceAdapter(settings()).fingerprint_for(
        RelevanceInput(summary="Сводка", interests=list(reversed(INTERESTS)))
    )
    policy = load_analysis_policy()
    changed = replace(policy, relevance=replace(policy.relevance, instructions="Изменено"))
    assert initial != OpenAIRelevanceAdapter(settings(), policy=changed).fingerprint_for(value)


@pytest.mark.parametrize("status", [401, 429, 500])
@pytest.mark.parametrize(
    "adapter_type,value",
    [
        (OpenAISummaryAdapter, unit()),
        (OpenAIImportanceAdapter, ImportanceInput(summary=SECRET_BODY)),
        (OpenAIRelevanceAdapter, RelevanceInput(summary=SECRET_BODY, interests=INTERESTS)),
    ],
)
def test_http_errors_are_sanitized(adapter_type: type, value: Any, status: int) -> None:
    with pytest.raises(LLMError) as error:
        run(
            adapter_type,
            value,
            lambda request: httpx.Response(status, text=f"{KEY} {SECRET_BODY} {INTERESTS}"),
        )
    assert str(status) in str(error.value)
    for secret in (KEY, SECRET_BODY, "космос"):
        assert secret not in str(error.value)


@pytest.mark.parametrize(
    "adapter_type,value",
    [
        (OpenAISummaryAdapter, unit()),
        (OpenAIImportanceAdapter, ImportanceInput(summary=SECRET_BODY)),
        (OpenAIRelevanceAdapter, RelevanceInput(summary=SECRET_BODY, interests=INTERESTS)),
    ],
)
@pytest.mark.parametrize(
    "kind",
    [
        "invalid_json",
        "empty",
        "incomplete",
        "refusal",
        "provider_error",
        "timeout",
        "transport",
    ],
)
def test_invalid_responses_and_transport_errors_are_sanitized(
    adapter_type: type, value: Any, kind: str
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if kind == "timeout":
            raise httpx.ReadTimeout(KEY)
        if kind == "transport":
            raise httpx.ConnectError(KEY)
        if kind == "invalid_json":
            return httpx.Response(200, text="not-json " + KEY)
        if kind == "empty":
            return httpx.Response(200, json=payload(""))
        if kind == "incomplete":
            return httpx.Response(200, json=payload(SECRET_BODY, status="incomplete"))
        if kind == "provider_error":
            return httpx.Response(
                200, json={**payload(SECRET_BODY), "error": {"message": KEY + SECRET_BODY}}
            )
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "output": [
                    {"type": "message", "content": [{"type": "refusal", "refusal": SECRET_BODY}]}
                ],
            },
        )

    with pytest.raises(LLMError) as error:
        run(adapter_type, value, handler)
    assert KEY not in str(error.value)
    assert SECRET_BODY not in str(error.value)


@pytest.mark.parametrize(
    "adapter_type,value,bad_json",
    [
        (
            OpenAIImportanceAdapter,
            ImportanceInput(summary=SECRET_BODY),
            '{"scale_score": 5, "significance_score": 1, "reaction_score": 0}',
        ),
        (
            OpenAIImportanceAdapter,
            ImportanceInput(summary=SECRET_BODY),
            '{"scale_score": 1.5, "significance_score": 1, "reaction_score": 0}',
        ),
        (OpenAIImportanceAdapter, ImportanceInput(summary=SECRET_BODY), "not-json"),
        (
            OpenAIRelevanceAdapter,
            RelevanceInput(summary=SECRET_BODY, interests=INTERESTS),
            '{"relevance": 1.5}',
        ),
        (
            OpenAIRelevanceAdapter,
            RelevanceInput(summary=SECRET_BODY, interests=INTERESTS),
            '{"relevance": true}',
        ),
        (
            OpenAIRelevanceAdapter,
            RelevanceInput(summary=SECRET_BODY, interests=INTERESTS),
            "not-json",
        ),
    ],
)
def test_structured_values_are_validated(adapter_type: type, value: Any, bad_json: str) -> None:
    with pytest.raises(LLMError) as error:
        run(adapter_type, value, lambda request: httpx.Response(200, json=payload(bad_json)))
    assert SECRET_BODY not in str(error.value)


def test_factory_defaults_and_independent_provider_selection() -> None:
    default = Settings(
        llm_provider="openrouter",
        importance_provider="typesafe",
        relevance_provider="typesafe",
        openai_api_key=SecretStr(""),
        llm_api_key=SecretStr("old-summary-key"),
        typesafe_api_key=SecretStr("old-typesafe-key"),
    )
    assert isinstance(create_summary_adapter(default), OpenRouterAdapter)
    assert isinstance(create_importance_adapter(default), JevImportanceAdapter)
    assert isinstance(create_relevance_adapter(default), JevRelevanceAdapter)
    assert isinstance(
        create_summary_adapter(settings(llm_api_key=SecretStr(""))), OpenAISummaryAdapter
    )
    assert isinstance(
        create_importance_adapter(settings(typesafe_api_key=SecretStr(""))), OpenAIImportanceAdapter
    )
    assert isinstance(
        create_relevance_adapter(settings(typesafe_api_key=SecretStr(""))), OpenAIRelevanceAdapter
    )
    assert create_summary_adapter(settings(), model="summary-override").model == "summary-override"
    assert (
        create_importance_adapter(settings(), model="importance-override").model
        == "importance-override"
    )
    assert (
        create_relevance_adapter(settings(), model="relevance-override").model
        == "relevance-override"
    )
    create_summary_adapter(default).validate_configuration()
    create_importance_adapter(default).validate_configuration()
    create_relevance_adapter(default).validate_configuration()
    with pytest.raises(LLMError):
        create_summary_adapter(settings(openai_api_key=SecretStr(""))).validate_configuration()
    with pytest.raises(LLMError):
        create_importance_adapter(settings(openai_api_key=SecretStr(""))).validate_configuration()
    with pytest.raises(LLMError):
        create_relevance_adapter(settings(openai_api_key=SecretStr(""))).validate_configuration()


def test_mixed_provider_configuration_selects_each_operation_independently() -> None:
    mixed = settings(
        llm_provider="openrouter",
        importance_provider="openai",
        relevance_provider="typesafe",
    )
    assert isinstance(create_summary_adapter(mixed), OpenRouterAdapter)
    assert isinstance(create_importance_adapter(mixed), OpenAIImportanceAdapter)
    assert isinstance(create_relevance_adapter(mixed), JevRelevanceAdapter)
    with pytest.raises(LLMError):
        create_importance_adapter(settings(importance_provider="unknown"))


@pytest.mark.parametrize(
    "factory,operation",
    [
        (create_summary_adapter, "summary"),
        (create_importance_adapter, "importance"),
        (create_relevance_adapter, "relevance"),
    ],
)
def test_model_config_precedence(factory, operation):
    configured = settings(openai_model="shared", **{f"openai_{operation}_model": "per-operation"})
    assert factory(configured).model == "per-operation"
    assert factory(configured, model="cli-override").model == "cli-override"
    assert factory(settings(openai_model="shared")).model == "shared"
    with pytest.raises(LLMError):
        factory(configured, model=" ")


def test_configuration_reads_env_file_without_changing_it(tmp_path, monkeypatch):
    keys = [
        "LLM_PROVIDER",
        "IMPORTANCE_PROVIDER",
        "RELEVANCE_PROVIDER",
        "OPENAI_API_KEY",
        "OPENAI_MODEL",
        "OPENAI_SUMMARY_MODEL",
        "OPENAI_IMPORTANCE_MODEL",
        "OPENAI_RELEVANCE_MODEL",
    ]
    for key in keys:
        monkeypatch.delenv(key, raising=False)
    env = tmp_path / ".env"
    original = (
        "LLM_PROVIDER=openai\nIMPORTANCE_PROVIDER=openai\nRELEVANCE_PROVIDER=typesafe\n"
        "OPENAI_API_KEY=test-local-key\nOPENAI_MODEL=base-model\n"
        "OPENAI_IMPORTANCE_MODEL=scoring-model\n"
    )
    env.write_text(original)
    config = Settings(_env_file=env)
    assert create_summary_adapter(config).model == "base-model"
    assert create_importance_adapter(config).model == "scoring-model"
    assert isinstance(create_relevance_adapter(config), JevRelevanceAdapter)
    assert "test-local-key" not in repr(config)
    monkeypatch.setenv("OPENAI_MODEL", "environment-model")
    assert create_summary_adapter(Settings(_env_file=env)).model == "environment-model"
    assert env.read_text() == original


@pytest.mark.parametrize("operation", ["summary", "importance", "relevance"])
def test_reuse_force_and_model_switch(operation):
    from app.ai.service import RelevanceService, SummarizationService

    async def exercise():
        calls = []

        def handler(request):
            calls.append(request)
            answer = {
                "summary": "Сводка",
                "importance": '{"scale_score": 2, "significance_score": 1, "reaction_score": 0}',
                "relevance": '{"relevance": 0.7}',
            }[operation]
            return httpx.Response(200, json=payload(answer))

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:

            def action(model):
                conf = settings(openai_model=model)
                if operation == "summary":
                    return SummarizationService(OpenAISummaryAdapter(conf, client=client)).summarize
                if operation == "importance":
                    return ImportanceService(
                        OpenAIImportanceAdapter(conf, client=client)
                    ).score_importance
                return RelevanceService(OpenAIRelevanceAdapter(conf, client=client)).score_relevance

            value = {
                "summary": unit(),
                "importance": ImportanceInput(summary="Сводка"),
                "relevance": RelevanceInput(summary="Сводка", interests=INTERESTS),
            }[operation]
            generate = action("first-model")
            first = await generate(value)
            reused = await generate(value, prior_result=first.result)
            assert reused.reused and len(calls) == 1
            assert reused.result == first.result
            forced = await generate(value, prior_result=first.result, force=True)
            assert not forced.reused and len(calls) == 2
            changed = await action("second-model")(value, prior_result=first.result)
            assert not changed.reused and len(calls) == 3
            assert changed.result.fingerprint != first.result.fingerprint

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "url",
    [
        "http://api.openai.com/v1",
        "https://user:pass@example.test/v1",
        "https://api.openai.com/v1?key=value",
        "https://api.openai.com/v1#fragment",
    ],
)
def test_unsafe_endpoint_rejected(url):
    with pytest.raises(LLMError):
        OpenAISummaryAdapter(settings(openai_base_url=url))


def test_redirect_is_not_followed_even_with_injected_client():
    async def exercise():
        calls = []

        def handler(request):
            calls.append(request)
            return httpx.Response(307, headers={"Location": "https://other.example/responses"})

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), follow_redirects=True
        ) as client:
            with pytest.raises(LLMError, match="307"):
                await OpenAISummaryAdapter(settings(), client=client).summarize(unit())
        assert len(calls) == 1

    asyncio.run(exercise())


@pytest.mark.parametrize("status", ["incomplete", "failed", "in_progress"])
def test_incomplete_message_never_saved_as_summary(status):
    response = payload("Частичный текст")
    response["output"][0]["status"] = status
    with pytest.raises(LLMError):
        run(OpenAISummaryAdapter, unit(), lambda _: httpx.Response(200, json=response))
