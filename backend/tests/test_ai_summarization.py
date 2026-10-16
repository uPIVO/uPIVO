import asyncio
import hashlib
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from app.ai.base import LLMError
from app.ai.dto import ContentUnitInput, SummaryResult, SummaryUsage
from app.ai.openrouter import OpenRouterAdapter
from app.ai.service import SummarizationService
from app.core.config import Settings

PRESET = "@preset/deep-seek-v4-flash-via-direct-deep-seek-provider"
API_KEY = "local-test-secret"


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "llm_provider": "openrouter",
        "llm_api_key": SecretStr(API_KEY),
        "llm_base_url": "https://openrouter.ai/api/v1",
        "llm_model": PRESET,
        "llm_timeout_seconds": 30,
    }
    values.update(overrides)
    return Settings(**values)


def _unit(body_text: str = "Публикация о запуске проекта") -> ContentUnitInput:
    return ContentUnitInput.model_validate(
        {
            "id": 41,
            "title": "Запуск проекта",
            "feed_items": [
                {
                    "feed_item_id": 7,
                    "title": "Первый источник",
                    "body_text": body_text,
                    "original_url": "https://example.test/one",
                    "published_at": "2026-10-16T12:00:00",
                    "source_name": "Источник один",
                    "is_primary": True,
                },
                {
                    "feed_item_id": 8,
                    "body_text": "Второй источник подтверждает запуск",
                    "original_url": "https://example.test/two",
                    "source_name": "Источник два",
                },
            ],
        }
    )


def _run(awaitable: Any) -> Any:
    return asyncio.run(awaitable)


class _FakeAdapter:
    def __init__(self, *, base_url: str = "https://openrouter.ai/api/v1") -> None:
        self.provider = "openrouter"
        self.preset = PRESET
        self.base_url = base_url
        self.prompt_version = "content-summary-v1"
        self.calls = 0

    def fingerprint_for(self, unit: ContentUnitInput) -> str:
        payload = {
            "version": "v1",
            "provider": self.provider,
            "base_url": self.base_url,
            "preset": self.preset,
            "prompt_version": self.prompt_version,
            "unit": unit.model_dump(mode="json"),
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return f"v1:{hashlib.sha256(encoded.encode()).hexdigest()}"

    async def summarize(self, unit: ContentUnitInput) -> SummaryResult:
        self.calls += 1
        return SummaryResult(
            summary="Оба источника сообщают о запуске проекта.",
            fingerprint=self.fingerprint_for(unit),
            provider=self.provider,
            preset=self.preset,
            usage=SummaryUsage(prompt_tokens=42, completion_tokens=11, total_tokens=53),
            cost_usd=Decimal("0.0007"),
        )


def test_content_unit_accepts_single_publication_without_optional_fields() -> None:
    unit = ContentUnitInput.model_validate(
        {
            "id": 1,
            "feed_items": [
                {
                    "feed_item_id": 3,
                    "body_text": "Одиночная публикация",
                    "original_url": "https://example.test/one",
                }
            ],
        }
    )

    assert len(unit.feed_items) == 1
    assert unit.feed_items[0].source_name is None
    assert unit.feed_items[0].extracted_entities is None


def test_content_unit_rejects_empty_units_and_blank_required_content() -> None:
    with pytest.raises(ValidationError):
        ContentUnitInput.model_validate({"id": 1, "feed_items": []})

    with pytest.raises(ValidationError):
        ContentUnitInput.model_validate(
            {
                "id": 1,
                "feed_items": [
                    {
                        "feed_item_id": 3,
                        "body_text": "  \n",
                        "original_url": "https://example.test/one",
                    }
                ],
            }
        )


@pytest.mark.parametrize("unit_id", [True, "1", 0])
def test_content_unit_requires_a_positive_integer_identifier(unit_id: Any) -> None:
    with pytest.raises(ValidationError):
        ContentUnitInput.model_validate(
            {
                "id": unit_id,
                "feed_items": [
                    {
                        "feed_item_id": 3,
                        "body_text": "Публикация",
                        "original_url": "https://example.test/one",
                    }
                ],
            }
        )


def test_service_reuses_only_matching_prior_result_and_force_regenerates() -> None:
    adapter = _FakeAdapter()
    service = SummarizationService(adapter)
    unit = _unit()

    generated = _run(service.summarize(unit))
    reused = _run(service.summarize(unit, prior_result=generated.result))
    changed = _run(
        service.summarize(_unit("Изменённый исходный текст"), prior_result=generated.result)
    )
    forced = _run(service.summarize(unit, prior_result=generated.result, force=True))

    assert generated.reused is False
    assert reused.reused is True
    assert reused.result == generated.result
    assert changed.reused is False
    assert forced.reused is False
    assert adapter.calls == 3


def test_service_invalidates_prior_result_when_adapter_configuration_changes() -> None:
    unit = _unit()
    first_adapter = _FakeAdapter()
    first = _run(SummarizationService(first_adapter).summarize(unit))
    changed_adapter = _FakeAdapter(base_url="https://other.example/api/v1")

    run = _run(SummarizationService(changed_adapter).summarize(unit, prior_result=first.result))

    assert run.reused is False
    assert changed_adapter.calls == 1


def test_service_validates_raw_input_before_calling_adapter() -> None:
    adapter = _FakeAdapter()
    service = SummarizationService(adapter)

    with pytest.raises(LLMError):
        _run(service.summarize({"id": 1, "feed_items": []}))

    assert adapter.calls == 0


@pytest.mark.parametrize(
    ("summary", "fingerprint"),
    [(" ", f"v1:{'a' * 64}"), ("Готовая выжимка", "неверный fingerprint")],
)
def test_service_rejects_invalid_prior_result_without_calling_adapter(
    summary: str, fingerprint: str
) -> None:
    adapter = _FakeAdapter()
    service = SummarizationService(adapter)
    prior_result = {
        "summary": summary,
        "fingerprint": fingerprint,
        "provider": "openrouter",
        "preset": PRESET,
    }

    with pytest.raises(LLMError):
        _run(service.summarize(_unit(), prior_result=prior_result))

    assert adapter.calls == 0


def test_openrouter_sends_every_publication_as_untrusted_json_and_returns_metadata() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "Оба источника подтверждают запуск."}}],
                "usage": {
                    "prompt_tokens": 64,
                    "completion_tokens": 12,
                    "total_tokens": 76,
                    "cost": 0.0009,
                },
            },
        )

    async def request_summary() -> SummaryResult:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            adapter = OpenRouterAdapter(_settings(), client=client)
            return await adapter.summarize(_unit())

    result = _run(request_summary())
    request = requests[0]
    payload = json.loads(request.content)
    prompt = payload["messages"][1]["content"]
    system_prompt = payload["messages"][0]["content"]

    assert "недоверенным содержимым" in system_prompt
    assert "расхождения" in system_prompt
    assert str(request.url) == "https://openrouter.ai/api/v1/chat/completions"
    assert request.headers["authorization"] == f"Bearer {API_KEY}"
    assert payload["model"] == PRESET
    assert "Источник один" in prompt
    assert "Источник два" in prompt
    assert "Второй источник подтверждает запуск" in prompt
    assert "Публикация о запуске проекта" in prompt
    assert "raw_payload" not in prompt
    assert result.summary == "Оба источника подтверждают запуск."
    assert result.fingerprint.startswith("v1:")
    assert result.usage is not None
    assert result.usage.total_tokens == 76
    assert result.cost_usd == Decimal("0.0009")



def test_openrouter_accepts_singleton_without_optional_publication_fields() -> None:
    requests: list[httpx.Request] = []
    unit = ContentUnitInput.model_validate(
        {
            "id": 5,
            "feed_items": [
                {
                    "feed_item_id": 21,
                    "body_text": "Одна нормализованная публикация",
                    "original_url": "https://example.test/single",
                }
            ],
        }
    )

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "Сервис запустили в октябре."}}]},
        )

    async def request_summary() -> SummaryResult:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await OpenRouterAdapter(_settings(), client=client).summarize(unit)

    result = _run(request_summary())
    payload = json.loads(requests[0].content)

    assert "Одна нормализованная публикация" in payload["messages"][1]["content"]
    assert result.summary == "Сервис запустили в октябре."
    assert result.usage is None
    assert result.cost_usd is None



def test_adapter_closes_its_http_client_after_a_request(monkeypatch: pytest.MonkeyPatch) -> None:
    clients: list[httpx.AsyncClient] = []

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "Выжимка"}}]})

    def make_client() -> httpx.AsyncClient:
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        clients.append(client)
        return client

    adapter = OpenRouterAdapter(_settings())
    monkeypatch.setattr(adapter, "_new_client", make_client)

    result = _run(adapter.summarize(_unit()))

    assert result.summary == "Выжимка"
    assert clients[0].is_closed


def test_documented_fixture_validates_as_content_unit() -> None:
    fixture = Path(__file__).resolve().parents[2] / "docs/examples/content-unit.json"
    unit = ContentUnitInput.model_validate(json.loads(fixture.read_text(encoding="utf-8")))

    assert len(unit.feed_items) == 2
    assert unit.feed_items[0].extracted_entities is not None


@pytest.mark.parametrize(
    ("status_code", "body"),
    [
        (429, "секретный ответ провайдера"),
        (503, "тело ответа не должно показываться"),
    ],
)
def test_openrouter_http_errors_are_sanitized(status_code: int, body: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, text=body)

    async def request_summary() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            adapter = OpenRouterAdapter(_settings(), client=client)
            await adapter.summarize(_unit("частный текст публикации"))

    with pytest.raises(LLMError) as exc_info:
        _run(request_summary())

    message = str(exc_info.value)
    assert API_KEY not in message
    assert body not in message
    assert "частный текст публикации" not in message


def test_openrouter_timeout_is_sanitized() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("секретное содержимое исключения", request=request)

    async def request_summary() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            adapter = OpenRouterAdapter(_settings(), client=client)
            await adapter.summarize(_unit("частный текст публикации"))

    with pytest.raises(LLMError) as exc_info:
        _run(request_summary())

    assert API_KEY not in str(exc_info.value)
    assert "секретное содержимое исключения" not in str(exc_info.value)
    assert "частный текст публикации" not in str(exc_info.value)



def test_openrouter_transport_error_is_sanitized() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("секретный текст ошибки", request=request)

    async def request_summary() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            adapter = OpenRouterAdapter(_settings(), client=client)
            await adapter.summarize(_unit("частный текст публикации"))

    with pytest.raises(LLMError) as exc_info:
        _run(request_summary())

    assert API_KEY not in str(exc_info.value)
    assert "секретный текст ошибки" not in str(exc_info.value)
    assert "частный текст публикации" not in str(exc_info.value)


def test_openrouter_fingerprint_covers_output_settings_and_excludes_secret() -> None:
    unit = _unit()
    default = OpenRouterAdapter(_settings())
    changed_input = OpenRouterAdapter(_settings())
    changed_preset = OpenRouterAdapter(_settings(llm_model="@preset/another-model"))
    changed_url = OpenRouterAdapter(_settings(llm_base_url="https://other.example/api/v1"))
    changed_key = OpenRouterAdapter(_settings(llm_api_key=SecretStr("different-secret")))

    fingerprint = default.fingerprint_for(unit)

    assert changed_input.fingerprint_for(_unit("Иной текст")) != fingerprint
    assert changed_preset.fingerprint_for(unit) != fingerprint
    assert changed_url.fingerprint_for(unit) != fingerprint
    assert changed_key.fingerprint_for(unit) == fingerprint


def test_openrouter_fingerprint_covers_prompt_version(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.ai.openrouter as openrouter

    adapter = OpenRouterAdapter(_settings())
    before = adapter.fingerprint_for(_unit())
    monkeypatch.setattr(openrouter, "PROMPT_VERSION", "content-summary-v3")

    assert adapter.fingerprint_for(_unit()) != before



def test_openrouter_fingerprint_covers_prompt_text(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.ai.openrouter as openrouter

    adapter = OpenRouterAdapter(_settings())
    before = adapter.fingerprint_for(_unit())
    monkeypatch.setattr(openrouter, "_SYSTEM_PROMPT", "Другая инструкция")

    assert adapter.fingerprint_for(_unit()) != before


def test_settings_reject_unbounded_network_timeout() -> None:
    with pytest.raises(ValidationError):
        _settings(llm_timeout_seconds=0)
    with pytest.raises(ValidationError):
        _settings(llm_timeout_seconds=301)


@pytest.mark.parametrize(
    "base_url",
    [
        "",
        "not-a-url",
        "http://example.test/api",
        "https://user:password@example.test/api",
        "https://example.test/?key=secret",
    ],
)
def test_openrouter_rejects_invalid_base_url_without_echoing_it(base_url: str) -> None:
    with pytest.raises(LLMError) as exc_info:
        OpenRouterAdapter(_settings(llm_base_url=base_url))

    assert not base_url or base_url not in str(exc_info.value)


def test_openrouter_requires_api_key_before_transport() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"choices": [{"message": {"content": "Выжимка"}}]})

    async def request_summary() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            adapter = OpenRouterAdapter(_settings(llm_api_key=SecretStr("")), client=client)
            await adapter.summarize(_unit())

    with pytest.raises(LLMError):
        _run(request_summary())

    assert calls == 0


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, text="не json"),
        httpx.Response(200, json={"choices": []}),
        httpx.Response(200, json={"choices": [{"message": {"content": "  "}}]}),
        httpx.Response(200, json={"choices": [{"message": {"content": ["не строка"]}}]}),
    ],
)
def test_openrouter_rejects_malformed_or_empty_responses(response: httpx.Response) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return response

    async def request_summary() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            adapter = OpenRouterAdapter(_settings(), client=client)
            await adapter.summarize(_unit())

    with pytest.raises(LLMError):
        _run(request_summary())



@pytest.mark.parametrize(
    ("finish_reason", "top_level_error", "message_error", "accepted"),
    [
        ("length", None, None, False),
        ("content_filter", None, None, False),
        ("error", None, None, False),
        ("stop", {"message": "секретное сообщение"}, None, False),
        ("stop", None, {"message": "секретное сообщение"}, False),
        ("stop", None, None, True),
        (None, None, None, True),
    ],
)
def test_openrouter_only_accepts_successful_completion(
    finish_reason: str | None,
    top_level_error: Any,
    message_error: Any,
    accepted: bool,
) -> None:
    message: dict[str, Any] = {"content": "Частичная выжимка"}
    if message_error is not None:
        message["error"] = message_error
    choice: dict[str, Any] = {"message": message}
    if finish_reason is not None:
        choice["finish_reason"] = finish_reason
    payload: dict[str, Any] = {"choices": [choice]}
    if top_level_error is not None:
        payload["error"] = top_level_error

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    async def request_summary() -> SummaryResult:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await OpenRouterAdapter(_settings(), client=client).summarize(_unit())

    if accepted:
        assert _run(request_summary()).summary == "Частичная выжимка"
    else:
        with pytest.raises(LLMError) as exc_info:
            _run(request_summary())
        assert "Частичная выжимка" not in str(exc_info.value)
        assert "секретное сообщение" not in str(exc_info.value)


def test_cli_prior_result_round_trip_is_database_free(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from app.ai import __main__ as cli

    input_path = tmp_path / "unit.json"
    saved_result = tmp_path / "summary.json"
    input_path.write_text(json.dumps(_unit().model_dump(mode="json")), encoding="utf-8")
    adapter = _FakeAdapter()
    monkeypatch.setattr(cli, "OpenRouterAdapter", lambda settings: adapter)

    first_exit = cli.main([str(input_path), "--save-result", str(saved_result)])
    first_output = json.loads(capsys.readouterr().out)
    second_exit = cli.main(
        [str(input_path), "--prior-result", str(saved_result), "--save-result", str(saved_result)]
    )
    second_output = json.loads(capsys.readouterr().out)

    assert first_exit == second_exit == 0
    assert first_output["status"] == "создан"
    assert second_output["status"] == "переиспользован"
    assert first_output["reused"] is False
    assert second_output["reused"] is True
    assert second_output["elapsed_seconds"] >= 0
    assert second_output["result"]["summary"] == first_output["result"]["summary"]
    assert second_output["result"]["usage"]["total_tokens"] == 53
    assert adapter.calls == 1



def test_cli_failed_force_preserves_existing_result_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from app.ai import __main__ as cli

    class _FailingAdapter(_FakeAdapter):
        async def summarize(self, unit: ContentUnitInput) -> SummaryResult:
            self.calls += 1
            raise LLMError("OpenRouter вернул пустую выжимку.")

    input_path = tmp_path / "unit.json"
    result_path = tmp_path / "summary.json"
    input_path.write_text(json.dumps(_unit().model_dump(mode="json")), encoding="utf-8")
    existing_result = _run(SummarizationService(_FakeAdapter()).summarize(_unit())).result
    result_path.write_text(existing_result.model_dump_json(indent=2) + "\n", encoding="utf-8")
    original_contents = result_path.read_bytes()
    failing_adapter = _FailingAdapter()
    monkeypatch.setattr(cli, "OpenRouterAdapter", lambda settings: failing_adapter)

    exit_code = cli.main(
        [
            str(input_path),
            "--prior-result",
            str(result_path),
            "--save-result",
            str(result_path),
            "--force",
        ]
    )
    stderr = capsys.readouterr().err

    assert exit_code == 2
    assert failing_adapter.calls == 1
    assert result_path.read_bytes() == original_contents
    assert "пустую выжимку" in stderr


def test_cli_rejects_invalid_json_without_echoing_fixture_contents(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from app.ai import __main__ as cli

    input_path = tmp_path / "unit.json"
    input_path.write_text("секретный текст {", encoding="utf-8")

    exit_code = cli.main([str(input_path)])
    stderr = capsys.readouterr().err

    assert exit_code != 0
    assert "секретный текст" not in stderr
    assert "JSON" in stderr
