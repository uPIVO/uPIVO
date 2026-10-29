from __future__ import annotations

import asyncio
import hashlib
import json
import math
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

import app.ai.importance as importance
from app.ai.analysis_policy import load_analysis_policy
from app.ai.base import LLMError
from app.ai.dto import (
    ImportanceComponents,
    ImportanceInput,
    ImportanceUsage,
    ImportanceWeights,
    SummaryResult,
)
from app.ai.jev import JevImportanceAdapter, JevRelevanceAdapter
from app.core.config import Settings

API_KEY = "test-typesafe-importance-key"
SUMMARY = "Синтетическая выжимка о большом изменении в отрасли и требуемых действиях."


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "typesafe_api_key": SecretStr(API_KEY),
        "typesafe_base_url": "https://api.typesafe.ai",
        "typesafe_model": "jev-1.14.0",
        "typesafe_timeout_seconds": 17,
        "llm_api_key": SecretStr(""),
    }
    values.update(overrides)
    return Settings(**values)


def _input(summary: str = SUMMARY) -> ImportanceInput:
    return ImportanceInput(summary=summary)


def _run(awaitable: Any) -> Any:
    return asyncio.run(awaitable)


def _score_answer(score: float, top_level: int) -> dict[str, Any]:
    lower = math.floor(score)
    upper = math.ceil(score)
    probabilities = {str(level): 0.0 for level in range(top_level + 1)}
    if lower == upper:
        probabilities[str(lower)] = 1.0
    else:
        probabilities[str(lower)] = upper - score
        probabilities[str(upper)] = score - lower
    return {
        "type": "score",
        "score": score,
        "legend": {str(level): f"Уровень {level}" for level in range(top_level + 1)},
        "probabilities": probabilities,
        "confidence": 0.8,
    }


def _response(
    *,
    scale_score: Any = 2.75,
    significance_score: Any = 3.25,
    reaction_score: Any = 1.5,
    model: str = "jev-1.14.0",
    usage: Any = None,
    omit: str | None = None,
    answer_type: str = "score",
) -> dict[str, Any]:
    answers: dict[str, Any] = {
        "scale": _score_answer(2.75, 4),
        "significance": _score_answer(3.25, 4),
        "reaction": _score_answer(1.5, 2),
    }
    answers["scale"]["score"] = scale_score
    answers["significance"]["score"] = significance_score
    answers["reaction"]["score"] = reaction_score
    if omit is not None:
        answers.pop(omit)
    if answer_type != "score":
        answers["significance"]["type"] = answer_type
    result: dict[str, Any] = {"model": model, "answers": answers}
    if usage is not None:
        result["usage"] = usage
    return result


class _FakeAdapter:
    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[ImportanceInput] = []
        self.fail = fail

    def fingerprint_for(self, value: ImportanceInput) -> str:
        return f"v1:{hashlib.sha256(value.summary.encode('utf-8')).hexdigest()}"

    async def score_importance(self, value: ImportanceInput) -> ImportanceComponents:
        self.calls.append(value)
        if self.fail:
            raise LLMError("Не удалось получить оценку общей важности.")
        return ImportanceComponents(
            scale_score=2.75,
            significance_score=3.25,
            reaction_score=1.5,
            fingerprint=self.fingerprint_for(value),
            provider="fake",
            model="fake-model",
            usage=ImportanceUsage(input_tokens=100, output_tokens=3),
        )


def test_typesafe_model_overrides_are_independent_and_do_not_mutate_settings() -> None:
    settings = _settings()
    relevance_default = JevRelevanceAdapter(settings)
    importance_default = JevImportanceAdapter(settings)
    relevance = JevRelevanceAdapter(settings, model="jev-relevance-override")
    importance_adapter = JevImportanceAdapter(settings, model="jev-importance-override")

    assert relevance_default.model == "jev-1.14.0"
    assert importance_default.model == "jev-1.14.0"
    assert relevance.model == "jev-relevance-override"
    assert importance_adapter.model == "jev-importance-override"
    assert settings.typesafe_model == "jev-1.14.0"
    for adapter_type in (JevRelevanceAdapter, JevImportanceAdapter):
        for model in ("", "   "):
            with pytest.raises(LLMError):
                adapter_type(settings, model=model)


def test_importance_rubric_is_loaded_in_order_with_numeric_rule_separate() -> None:
    policy = load_analysis_policy()

    assert policy.importance.prompt_version
    assert policy.importance.instructions.strip()
    assert [question.question_id for question in policy.importance.questions] == [
        "scale",
        "significance",
        "reaction",
    ]
    assert [len(question.criteria) for question in policy.importance.questions] == [5, 5, 3]
    assert all(question.prompt.strip() for question in policy.importance.questions)
    assert all(question.additional_instructions.strip() for question in policy.importance.questions)
    assert importance.IMPORTANCE_AGGREGATION_VERSION == "content-importance-sum-v2"
    assert importance.IMPORTANCE_WEIGHTS == ImportanceWeights(
        scale=1.0,
        significance=1.0,
        reaction=1.0,
    )


def test_jev_sends_three_score_questions_once_and_preserves_fractional_components() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json=_response(
                model="jev-importance-request-override",
                usage={"input_tokens": 456, "output_tokens": 3},
            ),
        )

    async def score() -> ImportanceComponents:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            adapter = JevImportanceAdapter(
                _settings(), model="jev-importance-request-override", client=client
            )
            return await adapter.score_importance(_input())

    result = _run(score())
    request = requests[0]
    payload = json.loads(request.content)
    policy = load_analysis_policy().importance
    expected_questions = {
        question.question_id: {
            "type": "score",
            "instructions": "\n\n".join(
                (policy.instructions, question.prompt, question.additional_instructions)
            ),
            "criteria": list(question.criteria),
        }
        for question in policy.questions
    }

    assert len(requests) == 1
    assert str(request.url) == "https://api.typesafe.ai/v1/systemone"
    assert request.headers["authorization"] == f"Bearer {API_KEY}"
    assert payload["model"] == "jev-importance-request-override"
    assert payload["state"] == SUMMARY
    assert payload["questions"] == expected_questions
    assert len(payload["questions"]) == 3
    assert all(question["type"] == "score" for question in payload["questions"].values())
    assert result.scale_score == 2.75
    assert result.significance_score == 3.25
    assert result.reaction_score == 1.5
    assert result.provider == "typesafe"
    assert result.model == "jev-importance-request-override"
    assert result.usage == ImportanceUsage(input_tokens=456, output_tokens=3)


def _invalid_score_body(question_id: str, value: Any) -> bytes:
    response = _response()
    response["answers"][question_id]["score"] = value
    return json.dumps(response, allow_nan=True).encode("utf-8")


@pytest.mark.parametrize(
    ("question_id", "value"),
    [
        ("scale", True),
        ("scale", "2.5"),
        ("scale", None),
        ("scale", -0.01),
        ("scale", 4.01),
        ("scale", float("nan")),
        ("significance", 4.01),
        ("reaction", -0.01),
        ("reaction", 2.01),
        ("reaction", float("inf")),
    ],
)
def test_jev_rejects_invalid_score_values_without_echoing_provider_data(
    question_id: str,
    value: Any,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=_invalid_score_body(question_id, value),
            headers={"Content-Type": "application/json"},
        )

    async def score() -> ImportanceComponents:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await JevImportanceAdapter(_settings(), client=client).score_importance(_input())

    with pytest.raises(LLMError) as error:
        _run(score())

    assert "некорректный ответ" in str(error.value)
    assert API_KEY not in str(error.value)
    assert SUMMARY not in str(error.value)
    assert repr(value) not in str(error.value)


@pytest.mark.parametrize(
    "response",
    [
        {},
        {"model": "jev-1.14.0", "answers": {}},
        {
            **_response(),
            "answers": {**_response()["answers"], "reaction": {"type": "noul", "noul": 0.8}},
        },
        {
            **_response(),
            "answers": {
                **_response()["answers"],
                "significance": {"type": "choice", "choice": "3"},
            },
        },
    ],
)
def test_jev_rejects_empty_or_wrong_typed_score_answers_without_echoing_response(
    response: dict[str, Any],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=response)

    async def score() -> ImportanceComponents:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await JevImportanceAdapter(_settings(), client=client).score_importance(_input())

    with pytest.raises(LLMError) as error:
        _run(score())

    assert "некорректный ответ" in str(error.value)
    assert SUMMARY not in str(error.value)
    assert API_KEY not in str(error.value)


@pytest.mark.parametrize("status", [401, 429, 529, 500])
def test_jev_importance_http_errors_are_sanitized_and_not_retried(status: int) -> None:
    requests: list[httpx.Request] = []
    raw_body = f"private body {SUMMARY} {API_KEY}"

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(status, text=raw_body)

    async def score() -> ImportanceComponents:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await JevImportanceAdapter(_settings(), client=client).score_importance(_input())

    with pytest.raises(LLMError) as error:
        _run(score())

    assert str(status) in str(error.value)
    assert raw_body not in str(error.value)
    assert API_KEY not in str(error.value)
    assert SUMMARY not in str(error.value)
    assert len(requests) == 1


def test_jev_importance_timeout_is_sanitized_without_retry() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        raise httpx.ReadTimeout(f"private {SUMMARY} {API_KEY}")

    async def score() -> ImportanceComponents:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await JevImportanceAdapter(_settings(), client=client).score_importance(_input())

    with pytest.raises(LLMError, match="Истекло время ожидания") as error:
        _run(score())

    assert len(requests) == 1
    assert SUMMARY not in str(error.value)
    assert API_KEY not in str(error.value)


def test_jev_importance_transport_error_is_sanitized_without_retry() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        raise httpx.ConnectError(f"private {SUMMARY} {API_KEY}")

    async def score() -> ImportanceComponents:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await JevImportanceAdapter(_settings(), client=client).score_importance(_input())

    with pytest.raises(LLMError, match="Не удалось подключиться") as error:
        _run(score())

    assert len(requests) == 1
    assert SUMMARY not in str(error.value)
    assert API_KEY not in str(error.value)


def test_jev_importance_requires_valid_key_before_transport() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(500)

    async def score() -> ImportanceComponents:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await JevImportanceAdapter(
                _settings(typesafe_api_key=SecretStr("")), client=client
            ).score_importance(_input())

    with pytest.raises(LLMError, match="API-ключ TypeSafe"):
        _run(score())

    assert requests == []


def test_importance_service_aggregates_fractional_components_and_keeps_versions() -> None:
    adapter = _FakeAdapter()
    run = _run(importance.ImportanceService(adapter).score_importance(_input()))

    assert run.reused is False
    assert run.result.scale_score == 2.75
    assert run.result.significance_score == 3.25
    assert run.result.reaction_score == 1.5
    assert run.result.total_score == 7.5
    assert run.result.weights == ImportanceWeights(scale=1.0, significance=1.0, reaction=1.0)
    assert run.result.aggregation_version == "content-importance-sum-v2"
    assert run.result.usage == ImportanceUsage(input_tokens=100, output_tokens=3)
    assert len(adapter.calls) == 1


def test_importance_service_reuses_only_matching_results_and_force_regenerates() -> None:
    adapter = _FakeAdapter()
    service = importance.ImportanceService(adapter)
    value = _input()

    generated = _run(service.score_importance(value))
    reused = _run(service.score_importance(value, prior_result=generated.result))
    changed = _run(
        service.score_importance(
            _input("Изменённая выжимка"),
            prior_result=generated.result,
        )
    )
    forced = _run(service.score_importance(value, prior_result=generated.result, force=True))

    assert generated.result.total_score == reused.result.total_score
    assert reused.reused is True
    assert reused.result == generated.result
    assert changed.reused is False
    assert forced.reused is False
    assert len(adapter.calls) == 3


def test_old_eight_point_aggregation_is_not_reused() -> None:
    adapter = _FakeAdapter()
    service = importance.ImportanceService(adapter)
    current = _run(service.score_importance(_input())).result
    old = current.model_copy(
        update={
            "total_score": 6.125,
            "weights": ImportanceWeights(scale=0.5, significance=1.0, reaction=1.0),
            "aggregation_version": "content-importance-weighted-v1",
        }
    )
    refreshed = _run(service.score_importance(_input(), prior_result=old))
    assert refreshed.reused is False
    assert refreshed.result.total_score == 7.5
    assert len(adapter.calls) == 2


def test_importance_service_rejects_invalid_input_and_prior_without_adapter_calls() -> None:
    adapter = _FakeAdapter()
    service = importance.ImportanceService(adapter)

    with pytest.raises(LLMError, match="Некорректные данные"):
        _run(service.score_importance({"summary": "  "}))
    with pytest.raises(LLMError, match="предыдущей оценки"):
        _run(
            service.score_importance(
                _input(),
                prior_result={"scale_score": True, "fingerprint": "invalid"},
            )
        )

    assert adapter.calls == []


def test_importance_fingerprint_covers_inputs_model_policy_and_aggregation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = JevImportanceAdapter(_settings())
    value = _input()
    initial = adapter.fingerprint_for(value)

    assert initial != adapter.fingerprint_for(_input(f"{SUMMARY} "))
    assert initial != JevImportanceAdapter(_settings(typesafe_model="jev-1.13.0")).fingerprint_for(
        value
    )
    explicit_override = JevImportanceAdapter(_settings(), model="jev-explicit-override")
    assert initial != explicit_override.fingerprint_for(value)
    assert initial != JevImportanceAdapter(
        _settings(typesafe_base_url="https://gateway.typesafe.example")
    ).fingerprint_for(value)
    assert initial == JevImportanceAdapter(
        _settings(typesafe_api_key=SecretStr("another-secret"))
    ).fingerprint_for(value)

    changed_instruction = replace(
        adapter.policy,
        importance=replace(
            adapter.policy.importance,
            instructions=f"{adapter.policy.importance.instructions} Дополнение.",
        ),
    )
    assert initial != JevImportanceAdapter(_settings(), policy=changed_instruction).fingerprint_for(
        value
    )
    changed_question = replace(
        adapter.policy,
        importance=replace(
            adapter.policy.importance,
            questions=(
                replace(
                    adapter.policy.importance.questions[0],
                    prompt=f"{adapter.policy.importance.questions[0].prompt} Изменение.",
                ),
                *adapter.policy.importance.questions[1:],
            ),
        ),
    )
    assert initial != JevImportanceAdapter(_settings(), policy=changed_question).fingerprint_for(
        value
    )
    changed_criteria = replace(
        adapter.policy,
        importance=replace(
            adapter.policy.importance,
            questions=(
                replace(
                    adapter.policy.importance.questions[0],
                    criteria=(
                        "Изменённый критерий",
                        *adapter.policy.importance.questions[0].criteria[1:],
                    ),
                ),
                *adapter.policy.importance.questions[1:],
            ),
        ),
    )
    assert initial != JevImportanceAdapter(_settings(), policy=changed_criteria).fingerprint_for(
        value
    )
    monkeypatch.setattr(importance, "IMPORTANCE_AGGREGATION_VERSION", "importance-v2")
    assert initial != adapter.fingerprint_for(value)
    aggregation_fingerprint = adapter.fingerprint_for(value)
    monkeypatch.setattr(
        importance,
        "IMPORTANCE_WEIGHTS",
        ImportanceWeights(scale=0.6, significance=1.0, reaction=1.0),
    )
    assert aggregation_fingerprint != adapter.fingerprint_for(value)


def test_importance_cli_reads_saved_summary_and_saves_metadata_without_echoing_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import app.ai.importance_cli as cli

    summary_path = tmp_path / "summary.json"
    result_path = tmp_path / "importance.json"
    summary_path.write_text(
        SummaryResult(
            summary=SUMMARY,
            fingerprint=f"v1:{'b' * 64}",
            provider="openrouter",
            preset="saved-preset",
        ).model_dump_json(),
        encoding="utf-8",
    )
    adapter = _FakeAdapter()
    selected_models: list[str | None] = []

    def make_adapter(settings: Settings, *, model: str | None = None) -> _FakeAdapter:
        selected_models.append(model)
        return adapter

    monkeypatch.setattr(cli, "JevImportanceAdapter", make_adapter)
    monkeypatch.setattr(cli, "get_settings", lambda: _settings())

    exit_code = cli.main(
        [str(summary_path), "--save-result", str(result_path), "--model", "importance-cli-override"]
    )
    output = capsys.readouterr()
    rendered = json.loads(output.out)
    saved = json.loads(result_path.read_text(encoding="utf-8"))

    assert exit_code == 0
    assert selected_models == ["importance-cli-override"]
    assert adapter.calls == [_input()]
    assert rendered["reused"] is False
    assert saved["scale_score"] == 2.75
    assert saved["significance_score"] == 3.25
    assert saved["reaction_score"] == 1.5
    assert saved["total_score"] == 7.5
    assert saved["weights"] == {"scale": 1.0, "significance": 1.0, "reaction": 1.0}
    assert saved["aggregation_version"] == "content-importance-sum-v2"
    assert SUMMARY not in output.out


def test_importance_cli_reuses_an_explicit_matching_prior_without_provider_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import app.ai.importance_cli as cli

    summary_path = tmp_path / "summary.json"
    prior_path = tmp_path / "importance.json"
    summary_path.write_text(
        SummaryResult(
            summary=SUMMARY,
            fingerprint=f"v1:{'b' * 64}",
            provider="openrouter",
            preset="saved-preset",
        ).model_dump_json(),
        encoding="utf-8",
    )
    prior = _run(importance.ImportanceService(_FakeAdapter()).score_importance(_input())).result
    prior_path.write_text(prior.model_dump_json(indent=2), encoding="utf-8")
    adapter = _FakeAdapter()
    monkeypatch.setattr(cli, "JevImportanceAdapter", lambda settings, model=None: adapter)
    monkeypatch.setattr(cli, "get_settings", lambda: _settings())

    exit_code = cli.main([str(summary_path), "--prior-result", str(prior_path)])
    output = capsys.readouterr()
    rendered = json.loads(output.out)

    assert exit_code == 0
    assert rendered["reused"] is True
    assert adapter.calls == []
    assert SUMMARY not in output.out


def test_importance_cli_force_failure_preserves_prior_and_summary_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import app.ai.importance_cli as cli

    summary_path = tmp_path / "summary.json"
    result_path = tmp_path / "importance.json"
    summary_path.write_text(
        SummaryResult(
            summary=SUMMARY,
            fingerprint=f"v1:{'b' * 64}",
            provider="openrouter",
            preset="saved-preset",
        ).model_dump_json(),
        encoding="utf-8",
    )
    old_result = _run(
        importance.ImportanceService(_FakeAdapter()).score_importance(_input())
    ).result
    result_path.write_text(old_result.model_dump_json(indent=2) + "\n", encoding="utf-8")
    original_summary = summary_path.read_bytes()
    original_result = result_path.read_bytes()
    adapter = _FakeAdapter(fail=True)
    monkeypatch.setattr(cli, "JevImportanceAdapter", lambda settings, model=None: adapter)
    monkeypatch.setattr(cli, "get_settings", lambda: _settings())

    exit_code = cli.main(
        [
            str(summary_path),
            "--prior-result",
            str(result_path),
            "--save-result",
            str(result_path),
            "--force",
        ]
    )
    output = capsys.readouterr()

    assert exit_code == 2
    assert len(adapter.calls) == 1
    assert summary_path.read_bytes() == original_summary
    assert result_path.read_bytes() == original_result
    assert SUMMARY not in output.err


def test_importance_cli_never_overwrites_summary_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import app.ai.importance_cli as cli

    summary_path = tmp_path / "summary.json"
    summary_path.write_text(
        SummaryResult(
            summary=SUMMARY,
            fingerprint=f"v1:{'b' * 64}",
            provider="openrouter",
            preset="saved-preset",
        ).model_dump_json(),
        encoding="utf-8",
    )
    original_summary = summary_path.read_bytes()
    adapter = _FakeAdapter()
    monkeypatch.setattr(cli, "JevImportanceAdapter", lambda settings, model=None: adapter)
    monkeypatch.setattr(cli, "get_settings", lambda: _settings())

    exit_code = cli.main([str(summary_path), "--save-result", str(summary_path)])
    output = capsys.readouterr()

    assert exit_code == 2
    assert not adapter.calls
    assert summary_path.read_bytes() == original_summary
    assert SUMMARY not in output.err


class _ModeledAdapter(_FakeAdapter):
    """Адаптер с моделью в отпечатке: смена модели меняет актуальность."""

    def __init__(self, model: str) -> None:
        super().__init__()
        self.model = model

    def fingerprint_for(self, value: ImportanceInput) -> str:
        seed = f"{self.model}:{value.summary}".encode()
        return f"v1:{hashlib.sha256(seed).hexdigest()}"


def test_requires_provider_call_shares_the_reuse_predicate() -> None:
    adapter = _FakeAdapter()
    service = importance.ImportanceService(adapter)
    value = _input()
    current = _run(service.score_importance(value)).result
    old = current.model_copy(
        update={
            "weights": ImportanceWeights(scale=0.5, significance=1.0, reaction=1.0),
            "aggregation_version": "content-importance-weighted-v1",
        }
    )

    assert service.requires_provider_call(value, prior_result=current) is False
    assert service.requires_provider_call(_input("Другая выжимка"), prior_result=current) is True
    assert service.requires_provider_call(value, prior_result=current, force=True) is True
    assert service.requires_provider_call(value) is True
    assert service.requires_provider_call(value, prior_result=old) is True
    assert service.requires_provider_call({"summary": "  "}, prior_result=current) is True
    assert service.requires_provider_call(value, prior_result={"summary": None}) is True

    reused = _run(service.score_importance(value, prior_result=current))

    assert reused.reused is True
    assert len(adapter.calls) == 1


def test_requires_provider_call_detects_model_change_only() -> None:
    first = _ModeledAdapter("model-a")
    service = importance.ImportanceService(first)
    value = _input()
    current = _run(service.score_importance(value)).result
    first.model = "model-b"

    assert service.requires_provider_call(value, prior_result=current) is True
    assert _run(service.score_importance(value, prior_result=current)).reused is False
    assert len(first.calls) == 2
