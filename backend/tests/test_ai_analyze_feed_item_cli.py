from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from app.ai.analyze_feed_item import main
from app.ai.dto import SummaryResult
from app.ai.repository import SaveDisposition
from app.services.analyze_feed_item import (
    AnalysisOperation,
    AnalysisStatus,
    AnalysisStepResult,
    FeedItemAnalysisOutcome,
)


class _FakeUseCase:
    outcome: FeedItemAnalysisOutcome
    calls: list[dict[str, Any]] = []

    def __init__(self, **_dependencies: Any) -> None:
        return None

    async def run(self, feed_item_id: int, **arguments: Any) -> FeedItemAnalysisOutcome:
        self.calls.append({"feed_item_id": feed_item_id, **arguments})
        return self.outcome


class _FakeAdapter:
    models: list[str | None] = []

    def __init__(self, _settings: Any, *, model: str | None = None) -> None:
        self.models.append(model)


def test_default_all_requires_user_and_interests_before_configuration_or_database(
    monkeypatch, capsys
) -> None:
    def unexpected() -> Any:
        raise AssertionError("До проверки обязательных аргументов конфигурацию читать нельзя")

    monkeypatch.setattr("app.ai.analyze_feed_item.get_settings", unexpected)

    assert main(["7"]) == 2
    assert "--user-id" in capsys.readouterr().err


def test_cli_emits_structured_result_without_echoing_interests(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    private_interest = "СЕКРЕТНЫЙ_ИНТЕРЕС"
    interests_path = tmp_path / "interests.json"
    interests_path.write_text(json.dumps([private_interest], ensure_ascii=False), encoding="utf-8")
    summary = SummaryResult(
        summary="Проверенная выжимка",
        fingerprint=f"v1:{hashlib.sha256(b'example').hexdigest()}",
        provider="openrouter",
        preset="@preset/test",
    )
    current = AnalysisStepResult(
        status=AnalysisStatus.CURRENT,
        reused=False,
        save_disposition=SaveDisposition.CURRENT,
        result=summary,
    )
    _FakeUseCase.outcome = FeedItemAnalysisOutcome(
        feed_item_id=7,
        operation=AnalysisOperation.ALL,
        attempt_count=1,
        summary=current,
        importance=AnalysisStepResult(status=AnalysisStatus.CURRENT, reused=True),
        relevance=AnalysisStepResult(status=AnalysisStatus.CURRENT, reused=False),
    )
    _FakeUseCase.calls.clear()
    monkeypatch.setattr("app.ai.analyze_feed_item.FeedItemAnalysisUseCase", _FakeUseCase)
    monkeypatch.setattr("app.ai.analyze_feed_item.get_settings", lambda: object())
    monkeypatch.setattr("app.ai.analyze_feed_item.get_engine", lambda: object())
    monkeypatch.setattr("app.ai.analyze_feed_item.OpenRouterAdapter", _FakeAdapter)
    monkeypatch.setattr("app.ai.analyze_feed_item.JevImportanceAdapter", _FakeAdapter)
    monkeypatch.setattr("app.ai.analyze_feed_item.JevRelevanceAdapter", _FakeAdapter)
    _FakeAdapter.models.clear()

    assert (
        main(
            [
                "7",
                "--user-id",
                "17",
                "--interests",
                str(interests_path),
                "--summary-model",
                "summary-override",
                "--importance-model",
                "importance-override",
                "--relevance-model",
                "relevance-override",
            ]
        )
        == 0
    )
    assert _FakeAdapter.models == [
        "summary-override",
        "importance-override",
        "relevance-override",
    ]

    output = capsys.readouterr().out
    parsed = json.loads(output)
    assert parsed["operation"] == "all"
    assert parsed["status"] == "current"
    assert parsed["attempt_count"] == 1
    assert parsed["operations"]["summary"]["result"]["summary"] == "Проверенная выжимка"
    assert private_interest not in output
    assert _FakeUseCase.calls[0]["interests"] == [private_interest]


def test_cli_constructs_only_adapters_for_selected_operation(monkeypatch, capsys) -> None:
    summary = SummaryResult(
        summary="Проверенная выжимка",
        fingerprint=f"v1:{hashlib.sha256(b'example').hexdigest()}",
        provider="openrouter",
        preset="summary-model",
    )
    _FakeUseCase.outcome = FeedItemAnalysisOutcome(
        feed_item_id=7,
        operation=AnalysisOperation.SUMMARY,
        attempt_count=1,
        summary=AnalysisStepResult(
            status=AnalysisStatus.CURRENT,
            reused=False,
            save_disposition=SaveDisposition.CURRENT,
            result=summary,
        ),
    )
    _FakeUseCase.calls.clear()
    _FakeAdapter.models.clear()
    monkeypatch.setattr("app.ai.analyze_feed_item.FeedItemAnalysisUseCase", _FakeUseCase)
    monkeypatch.setattr("app.ai.analyze_feed_item.get_settings", lambda: object())
    monkeypatch.setattr("app.ai.analyze_feed_item.get_engine", lambda: object())
    monkeypatch.setattr("app.ai.analyze_feed_item.OpenRouterAdapter", _FakeAdapter)

    def unexpected_adapter(*_arguments: Any, **_keywords: Any) -> None:
        raise AssertionError("Невыбранный AI-адаптер не должен создаваться")

    monkeypatch.setattr("app.ai.analyze_feed_item.JevImportanceAdapter", unexpected_adapter)
    monkeypatch.setattr("app.ai.analyze_feed_item.JevRelevanceAdapter", unexpected_adapter)

    assert (
        main(
            [
                "7",
                "--operation",
                "summary",
                "--summary-model",
                "summary-override",
                "--importance-model",
                "unused-importance-override",
                "--relevance-model",
                "unused-relevance-override",
            ]
        )
        == 0
    )
    assert _FakeAdapter.models == ["summary-override"]
    assert _FakeUseCase.calls[0]["operation"] == AnalysisOperation.SUMMARY
    capsys.readouterr()


def test_cli_sanitizes_invalid_interest_json_without_echoing_contents(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    private_content = "СЕКРЕТНЫЙ_ТЕКСТ_ИНТЕРЕСОВ"
    interests_path = tmp_path / "invalid.json"
    interests_path.write_text(private_content, encoding="utf-8")
    monkeypatch.setattr(
        "app.ai.analyze_feed_item.get_settings",
        lambda: (_ for _ in ()).throw(AssertionError("Конфигурация не должна загружаться")),
    )

    assert (
        main(
            ["7", "--operation", "relevance", "--user-id", "17", "--interests", str(interests_path)]
        )
        == 2
    )

    error = capsys.readouterr().err
    assert "интересов" in error
    assert private_content not in error
