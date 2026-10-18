from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.ai.analysis_policy import load_analysis_policy
from app.ai.base import LLMError


def _quote(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _policy_toml(*, criteria_counts: tuple[int, int, int] = (5, 5, 3)) -> str:
    lines = [
        "[summary]",
        'prompt_version = "summary-test-v1"',
        'system_prompt = "Тестовая инструкция summary"',
        'user_prompt_prefix = "Данные:"',
        "",
        "[relevance]",
        'prompt_version = "relevance-test-v1"',
        'question_id = "relevance"',
        'instructions = "Тестовая инструкция relevance"',
        "",
        "[importance]",
        'prompt_version = "importance-test-v1"',
        'instructions = "Тестовая инструкция importance"',
    ]
    for question_id, count in zip(
        ("scale", "significance", "reaction"), criteria_counts, strict=True
    ):
        lines.extend(
            [
                "",
                "[[importance.questions]]",
                f"id = {_quote(question_id)}",
                f"prompt = {_quote(f'Вопрос {question_id}')}",
                "criteria = ["
                + ", ".join(_quote(f"Критерий {index}") for index in range(count))
                + "]",
                f"additional_instructions = {_quote(f'Дополнение {question_id}')}",
            ]
        )
    return "\n".join(lines) + "\n"


def test_default_policy_has_required_sections_and_ordered_scales() -> None:
    policy = load_analysis_policy()

    assert policy.summary.prompt_version == "content-summary-v2"
    assert policy.summary.system_prompt.strip()
    assert policy.summary.user_prompt_prefix.strip()
    assert policy.relevance.prompt_version == "content-relevance-noul-v1"
    assert policy.relevance.question_id == "relevance"
    assert policy.relevance.instructions.strip()
    assert policy.importance.prompt_version == "content-importance-score-v1"
    assert policy.importance.instructions.strip()
    assert [question.question_id for question in policy.importance.questions] == [
        "scale",
        "significance",
        "reaction",
    ]
    assert [len(question.criteria) for question in policy.importance.questions] == [5, 5, 3]
    assert all(question.prompt.strip() for question in policy.importance.questions)
    assert all(question.additional_instructions.strip() for question in policy.importance.questions)


def test_default_policy_is_process_cached() -> None:
    assert load_analysis_policy() is load_analysis_policy()


def test_explicit_policy_path_is_loaded_and_validated(tmp_path: Path) -> None:
    path = tmp_path / "custom-policy.toml"
    path.write_text(_policy_toml(), encoding="utf-8")

    policy = load_analysis_policy(path)

    assert policy.summary.prompt_version == "summary-test-v1"
    assert tuple(question.question_id for question in policy.importance.questions) == (
        "scale",
        "significance",
        "reaction",
    )


@pytest.mark.parametrize(
    "content",
    [
        "not valid = [toml",
        _policy_toml(criteria_counts=(5, 4, 3)),
        _policy_toml(criteria_counts=(5, 5, 0)),
        _policy_toml().replace("[relevance]", "[other]"),
        _policy_toml().replace(
            'instructions = "Тестовая инструкция relevance"', 'instructions = "   "'
        ),
    ],
)
def test_invalid_policy_fails_with_safe_actionable_error(tmp_path: Path, content: str) -> None:
    path = tmp_path / "policy-with-private-name.toml"
    path.write_text(content, encoding="utf-8")

    with pytest.raises(LLMError) as error:
        load_analysis_policy(path)

    assert "analysis_policy.toml" in str(error.value)
    assert "summary" in str(error.value)
    assert "importance" in str(error.value)
    assert path.name not in str(error.value)


def test_missing_explicit_policy_fails_with_safe_error(tmp_path: Path) -> None:
    with pytest.raises(LLMError) as error:
        load_analysis_policy(tmp_path / "missing-private-policy.toml")

    assert "analysis_policy.toml" in str(error.value)
    assert "missing-private-policy.toml" not in str(error.value)
