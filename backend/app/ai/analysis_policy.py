from __future__ import annotations

import tomllib
from dataclasses import dataclass
from functools import lru_cache
from importlib.resources import files
from pathlib import Path
from typing import Any

from app.ai.base import LLMError

_POLICY_FILENAME = "analysis_policy.toml"
_IMPORTANCE_QUESTION_IDS = ("scale", "significance", "reaction")
_IMPORTANCE_CRITERIA_COUNTS = (5, 5, 3)


@dataclass(frozen=True, slots=True)
class SummaryPolicy:
    prompt_version: str
    system_prompt: str
    user_prompt_prefix: str


@dataclass(frozen=True, slots=True)
class RelevancePolicy:
    prompt_version: str
    question_id: str
    instructions: str


@dataclass(frozen=True, slots=True)
class ImportanceQuestionPolicy:
    question_id: str
    prompt: str
    criteria: tuple[str, ...]
    additional_instructions: str


@dataclass(frozen=True, slots=True)
class ImportancePolicy:
    prompt_version: str
    instructions: str
    questions: tuple[ImportanceQuestionPolicy, ...]


@dataclass(frozen=True, slots=True)
class AnalysisPolicy:
    summary: SummaryPolicy
    relevance: RelevancePolicy
    importance: ImportancePolicy


def _configuration_error() -> LLMError:
    return LLMError(
        "Не удалось загрузить корректную политику анализа из analysis_policy.toml. "
        "Проверьте разделы summary/relevance/importance, обязательные поля и шкалы "
        "importance (5/5/3 критерия)."
    )


def _nonblank(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def validate_analysis_policy(policy: AnalysisPolicy) -> AnalysisPolicy:
    try:
        if not isinstance(policy, AnalysisPolicy):
            raise _configuration_error()
        if not isinstance(policy.summary, SummaryPolicy):
            raise _configuration_error()
        if not isinstance(policy.relevance, RelevancePolicy):
            raise _configuration_error()
        if not isinstance(policy.importance, ImportancePolicy):
            raise _configuration_error()
        if not all(
            _nonblank(value)
            for value in (
                policy.summary.prompt_version,
                policy.summary.system_prompt,
                policy.summary.user_prompt_prefix,
                policy.relevance.prompt_version,
                policy.relevance.question_id,
                policy.relevance.instructions,
                policy.importance.prompt_version,
                policy.importance.instructions,
            )
        ):
            raise _configuration_error()
        if policy.relevance.question_id != "relevance":
            raise _configuration_error()
        if not isinstance(policy.importance.questions, tuple) or len(
            policy.importance.questions
        ) != len(_IMPORTANCE_QUESTION_IDS):
            raise _configuration_error()

        for question, expected_id, expected_criteria_count in zip(
            policy.importance.questions,
            _IMPORTANCE_QUESTION_IDS,
            _IMPORTANCE_CRITERIA_COUNTS,
            strict=True,
        ):
            if (
                not isinstance(question, ImportanceQuestionPolicy)
                or question.question_id != expected_id
                or not _nonblank(question.prompt)
                or not _nonblank(question.additional_instructions)
                or not isinstance(question.criteria, tuple)
                or len(question.criteria) != expected_criteria_count
                or not all(_nonblank(criterion) for criterion in question.criteria)
            ):
                raise _configuration_error()
    except (AttributeError, TypeError):
        raise _configuration_error() from None
    return policy


def _required_mapping(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError
    return value


def _required_text(mapping: dict[str, Any], key: str) -> str:
    value = mapping.get(key)
    if not _nonblank(value):
        raise ValueError
    return value


def _required_text_array(mapping: dict[str, Any], key: str) -> tuple[str, ...]:
    value = mapping.get(key)
    if not isinstance(value, list) or not all(_nonblank(item) for item in value):
        raise ValueError
    return tuple(value)


def _parse_importance_question(value: Any) -> ImportanceQuestionPolicy:
    question = _required_mapping(value)
    return ImportanceQuestionPolicy(
        question_id=_required_text(question, "id"),
        prompt=_required_text(question, "prompt"),
        criteria=_required_text_array(question, "criteria"),
        additional_instructions=_required_text(question, "additional_instructions"),
    )


def _parse_policy(raw: str) -> AnalysisPolicy:
    try:
        document = tomllib.loads(raw)
        summary_data = _required_mapping(document.get("summary"))
        relevance_data = _required_mapping(document.get("relevance"))
        importance_data = _required_mapping(document.get("importance"))
        question_data = importance_data.get("questions")
        if not isinstance(question_data, list):
            raise ValueError

        policy = AnalysisPolicy(
            summary=SummaryPolicy(
                prompt_version=_required_text(summary_data, "prompt_version"),
                system_prompt=_required_text(summary_data, "system_prompt"),
                user_prompt_prefix=_required_text(summary_data, "user_prompt_prefix"),
            ),
            relevance=RelevancePolicy(
                prompt_version=_required_text(relevance_data, "prompt_version"),
                question_id=_required_text(relevance_data, "question_id"),
                instructions=_required_text(relevance_data, "instructions"),
            ),
            importance=ImportancePolicy(
                prompt_version=_required_text(importance_data, "prompt_version"),
                instructions=_required_text(importance_data, "instructions"),
                questions=tuple(_parse_importance_question(question) for question in question_data),
            ),
        )
    except (KeyError, TypeError, ValueError, tomllib.TOMLDecodeError):
        raise _configuration_error() from None
    return validate_analysis_policy(policy)


@lru_cache(maxsize=1)
def _load_default_policy() -> AnalysisPolicy:
    try:
        raw = files("app.ai").joinpath(_POLICY_FILENAME).read_text(encoding="utf-8")
    except (OSError, ImportError, UnicodeError):
        raise _configuration_error() from None
    return _parse_policy(raw)


def load_analysis_policy(path: Path | None = None) -> AnalysisPolicy:
    if path is None:
        return _load_default_policy()
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        raise _configuration_error() from None
    return _parse_policy(raw)
