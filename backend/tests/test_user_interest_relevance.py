"""Персональная оценка по интересам из БД: правила отбора, гонки, фоновые хуки.

Проверки без живой PostgreSQL и без AI-провайдеров: каталог, репозиторий,
расписание задач, блокировка и провайдер просмотренности подменяются.
SQL-часть закрывают интеграционные проверки `test_user_interests_postgres.py`.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.ai.analysis_metadata import (
    FeedItemAIMetadata,
    StoredSummaryResult,
    SummaryResultHistory,
    content_input_fingerprint,
    interests_input_fingerprint,
)
from app.ai.base import LLMError
from app.ai.dto import RelevanceResult, SummaryResult
from app.ai.repository import SaveDisposition, content_unit_from_feed_item
from app.models.enums import SourceType
from app.models.feed_item import FeedItem
from app.models.source import Source
from app.models.user import User
from app.models.user_interest import UserInterest
from app.models.user_source import UserSource
from app.services.user_relevance import (
    ContentUnitRef,
    UnviewedCandidateSelector,
    UserRelevanceService,
    UserRelevanceStatus,
    notify_interests_changed,
    notify_source_activated,
)
from app.services.user_relevance_storage import InterestGuardedRepository


class _Session:
    """Сессия-заглушка: use case открывает её, guard блокирует строку users."""

    def __init__(self) -> None:
        self.statements: list[Any] = []

    def __enter__(self) -> _Session:
        return self

    def __exit__(self, *_arguments: Any) -> bool:
        return False

    def begin(self) -> _Session:
        return self

    def get(self, *_arguments: Any) -> object:
        return object()

    def execute(self, statement: Any, *_arguments: Any, **_kwargs: Any) -> None:
        self.statements.append(statement)


def _session_factory() -> _Session:
    return _Session()


def _summary(label: str = "Общая выжимка события") -> SummaryResult:
    return SummaryResult(
        summary=label, fingerprint=f"v1:{'a' * 64}", provider="fake", preset="test"
    )


def _relevance(value: float = 0.8) -> RelevanceResult:
    return RelevanceResult(
        relevance=value, fingerprint=f"v1:{'c' * 64}", provider="fake", model="test"
    )


class _Catalog:
    def __init__(
        self,
        *,
        interests: list[str] | None = None,
        ref: ContentUnitRef | None = None,
        access: bool = True,
        sources: list[int] | None = None,
        eligible: list[int] | None = None,
        candidates: list[ContentUnitRef] | None = None,
        interests_sequence: list[list[str]] | None = None,
    ) -> None:
        self.interests = interests or []
        self.interests_sequence = interests_sequence
        self.ref = ref if ref is not None else ContentUnitRef(kind="feed_item", unit_id=11)
        self.access = access
        self.sources = sources if sources is not None else [3]
        self.eligible = eligible or []
        self.candidates = candidates or []
        self.interest_reads = 0
        self.source_requests: list[int | None] = []
        self.eligible_sources: list[int] | None = None

    def load_user_interests(self, user_id: int) -> list[str]:
        self.interest_reads += 1
        if self.interests_sequence:
            index = min(self.interest_reads - 1, len(self.interests_sequence) - 1)
            return list(self.interests_sequence[index])
        return list(self.interests)

    def resolve_unit(self, kind: str, unit_id: int) -> ContentUnitRef | None:
        return self.ref

    def has_enabled_source_access(self, user_id: int, ref: ContentUnitRef) -> bool:
        return self.access

    def unit_source_ids(self, ref: ContentUnitRef) -> list[int]:
        return list(self.sources)

    def eligible_user_ids(self, source_ids: list[int]) -> list[int]:
        self.eligible_sources = list(source_ids)
        return list(self.eligible)

    def candidate_units(self, user_id: int, source_id: int | None = None) -> list[ContentUnitRef]:
        self.source_requests.append(source_id)
        return list(self.candidates)


class _Repository:
    """Подменяет хранилище оценок и ведёт себя как guard по актуальным интересам."""

    def __init__(
        self,
        *,
        summary_present: bool = True,
        prior: RelevanceResult | None = None,
        current_interests: Any = None,
        always_stale: bool = False,
    ) -> None:
        self.summary = _summary()
        self.snapshot = SimpleNamespace(
            unit=SimpleNamespace(id=11),
            input_fingerprint=f"input-v1:{'b' * 64}",
            summary_result=self.summary if summary_present else None,
            importance_result=None,
        )
        self.prior = prior
        self.current_interests = current_interests
        self.always_stale = always_stale
        self.stale_saves = 0
        self.saved: list[dict[str, Any]] = []
        self.prior_requests: list[dict[str, Any]] = []

    def load_snapshot(self, _session: Any, *, kind: str, unit_id: int) -> Any:
        return self.snapshot

    def get_relevance_prior(self, _session: Any, **kwargs: Any) -> RelevanceResult | None:
        self.prior_requests.append(kwargs)
        return self.prior

    def save_relevance(self, _session: Any, **kwargs: Any) -> SaveDisposition:
        if self.always_stale:
            self.stale_saves += 1
            return SaveDisposition.STALE_CANDIDATE
        if self.current_interests is not None:
            current = interests_input_fingerprint(self.current_interests())
            if kwargs["interests_fingerprint"] != current:
                self.stale_saves += 1
                return SaveDisposition.STALE_CANDIDATE
        self.saved.append(kwargs)
        return SaveDisposition.CURRENT


class _ScoringService:
    def __init__(
        self,
        *,
        error: Exception | None = None,
        relevance: float = 0.8,
    ) -> None:
        self.error = error
        self.result = _relevance(relevance)
        self.inputs: list[dict[str, Any]] = []
        self.provider_calls = 0

    def validate_configuration(self) -> None:
        return None

    async def score_relevance(
        self,
        value: dict[str, Any],
        *,
        prior_result: Any,
        force: bool,
    ) -> Any:
        self.inputs.append({"value": value, "prior_result": prior_result})
        if self.error is not None:
            raise self.error
        if prior_result is not None and not force:
            return SimpleNamespace(result=prior_result, reused=True)
        self.provider_calls += 1
        return SimpleNamespace(result=self.result, reused=False)


class _Scheduler:
    def __init__(self) -> None:
        self.content: list[tuple[int, str, int]] = []
        self.users: list[int] = []
        self.sources: list[tuple[int, int]] = []

    def schedule_content_unit(self, *, user_id: int, kind: str, unit_id: int) -> None:
        self.content.append((user_id, kind, unit_id))

    def schedule_user_recalculation(self, *, user_id: int) -> None:
        self.users.append(user_id)

    def schedule_source_recalculation(self, *, user_id: int, source_id: int) -> None:
        self.sources.append((user_id, source_id))


class _Lock:
    def __init__(self, *, acquired: bool = True) -> None:
        self.acquired = acquired
        self.held: list[tuple[int, str, int]] = []
        self.released: list[tuple[int, str, int]] = []

    @contextmanager
    def hold(self, *, user_id: int, kind: str, unit_id: int):
        self.held.append((user_id, kind, unit_id))
        try:
            yield self.acquired
        finally:
            self.released.append((user_id, kind, unit_id))


class _Provider:
    def __init__(self, viewed: dict[tuple[str, int], bool] | None = None) -> None:
        self.viewed = viewed or {}
        self.calls: list[tuple[int, str, int]] = []

    def was_viewed(self, user_id: int, kind: str, unit_id: int) -> bool:
        self.calls.append((user_id, kind, unit_id))
        return self.viewed.get((kind, unit_id), False)


def _service(
    *,
    catalog: _Catalog,
    repository: _Repository | None = None,
    scoring: _ScoringService | None = None,
    scheduler: _Scheduler | None = None,
    provider: _Provider | None = None,
    lock: _Lock | None = None,
    selector: UnviewedCandidateSelector | None = None,
) -> UserRelevanceService:
    return UserRelevanceService(
        session_factory=_session_factory,
        relevance_service_factory=lambda: scoring or _ScoringService(),
        repository=repository or _Repository(),
        catalog=catalog,
        scheduler=scheduler or _Scheduler(),
        viewed_status_provider=provider,
        selector=selector,
        lock=lock if lock is not None else _Lock(),
    )


def test_empty_interests_skip_without_ai_call() -> None:
    catalog = _Catalog(interests=[])
    scoring = _ScoringService()
    lock = _Lock()
    service = _service(catalog=catalog, scoring=scoring, lock=lock)

    outcome = asyncio.run(service.score_user_content_unit(7, "feed_item", 11))

    assert outcome.status is UserRelevanceStatus.SKIPPED
    assert scoring.inputs == []
    assert lock.held == []


def test_missing_current_summary_skips_without_ai_call() -> None:
    catalog = _Catalog(interests=["ИИ"])
    scoring = _ScoringService()
    repository = _Repository(summary_present=False)
    service = _service(catalog=catalog, repository=repository, scoring=scoring)

    outcome = asyncio.run(service.score_user_content_unit(7, "feed_item", 11))

    assert outcome.status is UserRelevanceStatus.SKIPPED
    assert scoring.inputs == []
    assert outcome.unit_id == 11


def test_content_without_enabled_source_is_skipped_without_ai_call() -> None:
    catalog = _Catalog(interests=["ИИ"], access=False)
    scoring = _ScoringService()
    repository = _Repository()
    service = _service(catalog=catalog, repository=repository, scoring=scoring)

    outcome = asyncio.run(service.score_user_content_unit(7, "feed_item", 11))

    assert outcome.status is UserRelevanceStatus.SKIPPED
    assert scoring.inputs == []
    assert repository.saved == []


def test_feed_item_inside_cluster_is_scored_once_as_cluster() -> None:
    catalog = _Catalog(interests=["ИИ"], ref=ContentUnitRef(kind="cluster", unit_id=44))
    scoring = _ScoringService()
    lock = _Lock()
    service = _service(catalog=catalog, scoring=scoring, lock=lock)

    outcome = asyncio.run(service.score_user_content_unit(7, "feed_item", 11))

    assert outcome.kind == "cluster"
    assert outcome.unit_id == 44
    assert outcome.status is UserRelevanceStatus.CURRENT
    assert lock.held == [(7, "cluster", 44)]
    assert scoring.provider_calls == 1


def test_full_interest_list_is_scored_once_per_unit() -> None:
    catalog = _Catalog(interests=["ИИ-агенты", "Электромобили"])
    scoring = _ScoringService()
    repository = _Repository()
    service = _service(catalog=catalog, repository=repository, scoring=scoring)

    outcome = asyncio.run(service.score_user_content_unit(7, "feed_item", 11))

    assert outcome.status is UserRelevanceStatus.CURRENT
    assert len(scoring.inputs) == 1
    assert scoring.provider_calls == 1
    assert scoring.inputs[0]["value"]["interests"] == ["ИИ-агенты", "Электромобили"]
    assert repository.saved[0]["interests_fingerprint"] == interests_input_fingerprint(
        ["ИИ-агенты", "Электромобили"]
    )


def test_actual_result_is_reused_without_second_provider_call() -> None:
    prior = _relevance()
    catalog = _Catalog(interests=["ИИ"])
    scoring = _ScoringService()
    repository = _Repository(prior=prior)
    service = _service(catalog=catalog, repository=repository, scoring=scoring)

    outcome = asyncio.run(service.score_user_content_unit(7, "feed_item", 11))

    assert outcome.status is UserRelevanceStatus.CURRENT
    assert outcome.reused is True
    assert scoring.provider_calls == 0
    assert scoring.inputs[0]["prior_result"] is prior
    assert repository.saved[0]["interests_fingerprint"] == interests_input_fingerprint(["ИИ"])


def test_stale_interests_are_reread_and_old_answer_is_not_saved_as_current() -> None:
    catalog = _Catalog(interests_sequence=[["ИИ"], ["ИИ"], ["ИИ", "Роботы"]])
    scoring = _ScoringService()
    repository = _Repository(current_interests=lambda: ["ИИ", "Роботы"])
    service = _service(catalog=catalog, repository=repository, scoring=scoring)

    outcome = asyncio.run(service.score_user_content_unit(7, "feed_item", 11))

    assert outcome.status is UserRelevanceStatus.CURRENT
    assert outcome.attempts == 2
    # Внутренний повтор use case отключён в DB-пути: гонка A→B стоит ровно два
    # AI-вызова (A и B), а не A, A, B.
    assert repository.stale_saves == 1
    assert scoring.provider_calls == 2
    assert [entry["value"]["interests"] for entry in scoring.inputs] == [
        ["ИИ"],
        ["ИИ", "Роботы"],
    ]
    assert [entry["interests_fingerprint"] for entry in repository.saved] == [
        interests_input_fingerprint(["ИИ", "Роботы"])
    ]


def test_interests_removed_during_ai_call_skip_without_extra_provider_call() -> None:
    catalog = _Catalog(interests_sequence=[["ИИ"], ["ИИ"], []])
    scoring = _ScoringService()
    repository = _Repository(always_stale=True)
    service = _service(catalog=catalog, repository=repository, scoring=scoring)

    outcome = asyncio.run(service.score_user_content_unit(7, "feed_item", 11))

    assert outcome.status is UserRelevanceStatus.SKIPPED
    assert scoring.provider_calls == 1
    assert repository.stale_saves == 1
    assert repository.saved == []


def test_stale_interests_that_keep_changing_do_not_produce_current_result() -> None:
    catalog = _Catalog(interests=["ИИ"])
    repository = _Repository(always_stale=True)
    scoring = _ScoringService()
    service = _service(catalog=catalog, repository=repository, scoring=scoring)

    outcome = asyncio.run(service.score_user_content_unit(7, "feed_item", 11))

    assert outcome.status is UserRelevanceStatus.FAILED
    assert outcome.attempts == 2
    assert outcome.error
    assert scoring.provider_calls == 2
    assert repository.saved == []


def test_ai_failure_is_isolated_and_does_not_write_score() -> None:
    catalog = _Catalog(interests=["ИИ"])
    scoring = _ScoringService(error=LLMError("Провайдер недоступен"))
    repository = _Repository()
    service = _service(catalog=catalog, repository=repository, scoring=scoring)

    outcome = asyncio.run(service.score_user_content_unit(7, "feed_item", 11))

    assert outcome.status is UserRelevanceStatus.FAILED
    assert outcome.error == "Провайдер недоступен"
    assert repository.saved == []


def test_lock_is_released_and_prevents_duplicate_ai_call() -> None:
    catalog = _Catalog(interests=["ИИ"])
    scoring = _ScoringService()
    busy = _Lock(acquired=False)
    service = _service(catalog=catalog, scoring=scoring, lock=busy)

    skipped = asyncio.run(service.score_user_content_unit(7, "feed_item", 11))

    assert skipped.status is UserRelevanceStatus.SKIPPED
    assert scoring.inputs == []
    assert busy.released == [(7, "feed_item", 11)]

    free = _Lock()
    second = _service(catalog=catalog, scoring=scoring, lock=free)
    done = asyncio.run(second.score_user_content_unit(7, "feed_item", 11))

    assert done.status is UserRelevanceStatus.CURRENT
    assert free.released == [(7, "feed_item", 11)]


def test_eligible_users_are_only_scheduled_in_background() -> None:
    catalog = _Catalog(
        interests=[],
        ref=ContentUnitRef(kind="cluster", unit_id=44),
        sources=[3, 4],
        eligible=[7, 9],
    )
    scheduler = _Scheduler()
    scoring = _ScoringService()
    service = _service(catalog=catalog, scoring=scoring, scheduler=scheduler)

    scheduled = service.score_content_unit_for_eligible_users("feed_item", 11)

    assert scheduled == [7, 9]
    assert catalog.eligible_sources == [3, 4]
    assert scheduler.content == [(7, "cluster", 44), (9, "cluster", 44)]
    assert scoring.inputs == []


def test_unit_without_sources_schedules_nobody() -> None:
    catalog = _Catalog(sources=[])
    scheduler = _Scheduler()
    service = _service(catalog=catalog, scheduler=scheduler)

    assert service.score_content_unit_for_eligible_users("feed_item", 11) == []
    assert scheduler.content == []


def _selector(
    *,
    provider: _Provider,
    with_summary: set[tuple[str, int]] | None = None,
) -> UnviewedCandidateSelector:
    available = with_summary or set()

    def has_current_summary(ref: ContentUnitRef) -> bool:
        return (ref.kind, ref.unit_id) in available

    return UnviewedCandidateSelector(
        viewed_status_provider=provider, has_current_summary=has_current_summary
    )


def test_recalculation_without_provider_does_not_touch_history() -> None:
    catalog = _Catalog(
        interests=["ИИ"],
        candidates=[ContentUnitRef(kind="feed_item", unit_id=11)],
    )
    scheduler = _Scheduler()
    service = _service(catalog=catalog, scheduler=scheduler, provider=None)

    assert service.recalculate_unviewed_for_user(7) == []
    assert scheduler.content == []
    assert catalog.source_requests == []


def test_recalculation_skips_viewed_units_and_keeps_cluster_as_one_unit() -> None:
    provider = _Provider({("feed_item", 11): True, ("cluster", 44): True})
    candidates = [
        ContentUnitRef(kind="feed_item", unit_id=11),
        ContentUnitRef(kind="feed_item", unit_id=12),
        ContentUnitRef(kind="cluster", unit_id=44),
        ContentUnitRef(kind="cluster", unit_id=45),
    ]
    catalog = _Catalog(interests=["ИИ"], candidates=candidates)
    scheduler = _Scheduler()
    selector = _selector(
        provider=provider,
        with_summary={(ref.kind, ref.unit_id) for ref in candidates},
    )
    service = _service(catalog=catalog, scheduler=scheduler, provider=provider, selector=selector)

    selected = service.recalculate_unviewed_for_user(7)

    assert selected == [
        ContentUnitRef(kind="feed_item", unit_id=12),
        ContentUnitRef(kind="cluster", unit_id=45),
    ]
    assert scheduler.content == [(7, "feed_item", 12), (7, "cluster", 45)]
    assert ("feed_item", 12) in [(kind, unit_id) for _, kind, unit_id in provider.calls]


def test_viewed_cluster_excludes_members_from_scoring() -> None:
    provider = _Provider({("cluster", 44): True})
    catalog = _Catalog(
        interests=["ИИ"],
        candidates=[ContentUnitRef(kind="cluster", unit_id=44)],
    )
    scheduler = _Scheduler()
    selector = _selector(provider=provider, with_summary={("cluster", 44)})
    service = _service(catalog=catalog, scheduler=scheduler, provider=provider, selector=selector)

    assert service.recalculate_unviewed_for_user(7) == []
    assert scheduler.content == []
    assert [kind for _user_id, kind, _unit_id in provider.calls] == ["cluster"]


def test_candidate_without_current_summary_is_not_scheduled() -> None:
    provider = _Provider()
    catalog = _Catalog(
        interests=["ИИ"],
        candidates=[ContentUnitRef(kind="feed_item", unit_id=11)],
    )
    scheduler = _Scheduler()
    selector = _selector(provider=provider, with_summary=set())
    service = _service(catalog=catalog, scheduler=scheduler, provider=provider, selector=selector)

    assert service.recalculate_unviewed_for_user(7) == []
    assert scheduler.content == []


def test_recalculation_without_interests_schedules_nothing() -> None:
    provider = _Provider()
    catalog = _Catalog(interests=[], candidates=[ContentUnitRef(kind="feed_item", unit_id=11)])
    scheduler = _Scheduler()
    selector = _selector(provider=provider, with_summary={("feed_item", 11)})
    service = _service(catalog=catalog, scheduler=scheduler, provider=provider, selector=selector)

    assert service.recalculate_unviewed_for_user(7) == []
    assert scheduler.content == []


def test_source_activation_schedules_only_the_source_scope() -> None:
    provider = _Provider()
    catalog = _Catalog(
        interests=["ИИ"],
        candidates=[ContentUnitRef(kind="feed_item", unit_id=11)],
    )
    scheduler = _Scheduler()
    selector = _selector(provider=provider, with_summary={("feed_item", 11)})
    service = _service(catalog=catalog, scheduler=scheduler, provider=provider, selector=selector)

    selected = service.recalculate_unviewed_source_for_user(7, 3)

    assert selected == [ContentUnitRef(kind="feed_item", unit_id=11)]
    assert catalog.source_requests == [3]
    assert scheduler.content == [(7, "feed_item", 11)]


def test_hooks_schedule_background_work_only_with_viewed_provider() -> None:
    catalog = _Catalog(interests=["ИИ"])
    scheduler = _Scheduler()
    without_provider = _service(catalog=catalog, scheduler=scheduler, provider=None)

    assert notify_interests_changed(7, service=without_provider) is False
    assert notify_source_activated(7, 3, service=without_provider) is False
    assert scheduler.users == []
    assert scheduler.sources == []

    with_provider = _service(catalog=catalog, scheduler=scheduler, provider=_Provider())

    assert notify_interests_changed(7, service=with_provider) is True
    assert notify_source_activated(7, 3, service=with_provider) is True
    assert scheduler.users == [7]
    assert scheduler.sources == [(7, 3)]


def test_default_hooks_do_nothing_without_real_provider() -> None:
    assert notify_interests_changed(7) is False
    assert notify_source_activated(7, 3) is False


class _GuardSession(_Session):
    """Guard блокирует строку users; остальное заменяют внедрённые функции."""


def test_guard_rejects_answer_for_outdated_interests(monkeypatch: pytest.MonkeyPatch) -> None:
    delegated: list[dict[str, Any]] = []

    def fake_save(self: Any, _session: Any, **kwargs: Any) -> SaveDisposition:
        delegated.append(kwargs)
        return SaveDisposition.CURRENT

    monkeypatch.setattr(
        "app.ai.repository.ContentUnitAnalysisRepository.save_relevance", fake_save
    )
    repository = InterestGuardedRepository(
        interests_reader=lambda _session, _user_id: ["Роботы"],
        access_checker=lambda _session, **_kwargs: True,
    )

    disposition = repository.save_relevance(
        _GuardSession(),
        user_id=7,
        kind="feed_item",
        unit_id=11,
        source_input_fingerprint=f"input-v1:{'b' * 64}",
        expected_summary_lineage_fingerprint=f"summary-lineage-v1:{'c' * 64}",
        interests_fingerprint=interests_input_fingerprint(["ИИ"]),
        result=_relevance(),
    )

    assert disposition is SaveDisposition.STALE_CANDIDATE
    assert delegated == []


def test_guard_rejects_content_outside_enabled_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    delegated: list[dict[str, Any]] = []

    def fake_save(self: Any, _session: Any, **kwargs: Any) -> SaveDisposition:
        delegated.append(kwargs)
        return SaveDisposition.CURRENT

    monkeypatch.setattr(
        "app.ai.repository.ContentUnitAnalysisRepository.save_relevance", fake_save
    )
    repository = InterestGuardedRepository(
        interests_reader=lambda _session, _user_id: ["ИИ"],
        access_checker=lambda _session, **_kwargs: False,
    )

    disposition = repository.save_relevance(
        _GuardSession(),
        user_id=7,
        kind="feed_item",
        unit_id=11,
        source_input_fingerprint=f"input-v1:{'b' * 64}",
        expected_summary_lineage_fingerprint=f"summary-lineage-v1:{'c' * 64}",
        interests_fingerprint=interests_input_fingerprint(["ИИ"]),
        result=_relevance(),
    )

    assert disposition is SaveDisposition.NOT_FOUND
    assert delegated == []


def test_guard_saves_current_answer_for_current_interests(monkeypatch: pytest.MonkeyPatch) -> None:
    delegated: list[dict[str, Any]] = []

    def fake_save(self: Any, _session: Any, **kwargs: Any) -> SaveDisposition:
        delegated.append(kwargs)
        return SaveDisposition.CURRENT

    monkeypatch.setattr(
        "app.ai.repository.ContentUnitAnalysisRepository.save_relevance", fake_save
    )
    repository = InterestGuardedRepository(
        interests_reader=lambda _session, _user_id: ["ИИ"],
        access_checker=lambda _session, **_kwargs: True,
    )

    disposition = repository.save_relevance(
        _GuardSession(),
        user_id=7,
        kind="feed_item",
        unit_id=11,
        source_input_fingerprint=f"input-v1:{'b' * 64}",
        expected_summary_lineage_fingerprint=f"summary-lineage-v1:{'c' * 64}",
        interests_fingerprint=interests_input_fingerprint(["ИИ"]),
        result=_relevance(),
    )

    assert disposition is SaveDisposition.CURRENT
    assert len(delegated) == 1
    assert delegated[0]["interests_fingerprint"] == interests_input_fingerprint(["ИИ"])


def test_celery_tasks_are_registered_for_the_service_contract() -> None:
    from app.workers import tasks  # noqa: F401  # импорт регистрирует задачи
    from app.workers.celery_app import celery_app

    assert {
        "relevance.score_user_content_unit",
        "relevance.score_content_unit_for_eligible_users",
        "relevance.recalculate_user",
        "relevance.recalculate_user_source",
    } <= set(celery_app.tasks)


def test_background_task_runs_service_and_returns_json_safe_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.ai.repository import SaveDisposition  # noqa: F401  # контракт репозитория
    from app.workers import tasks

    class _TaskService:
        async def score_user_content_unit(self, user_id: int, kind: str, unit_id: int):
            return SimpleNamespace(
                user_id=user_id,
                kind=kind,
                unit_id=unit_id,
                status=UserRelevanceStatus.CURRENT,
                attempts=1,
                reused=False,
                error=None,
            )

    monkeypatch.setattr(tasks, "build_default_service", lambda **_kwargs: _TaskService())

    result = tasks.score_user_content_unit_task(7, "feed_item", 11)

    assert result == {
        "user_id": 7,
        "kind": "feed_item",
        "unit_id": 11,
        "status": "current",
        "attempts": 1,
        "reused": False,
        "error": None,
    }


class _RecordingSendTask:
    """Заменяет брокер: запоминает поставленные фоновые задачи."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, dict[str, Any]]] = []

    def send_task(self, name: str, kwargs: dict[str, Any] | None = None) -> None:
        self.sent.append((name, dict(kwargs or {})))


_RELEVANCE_TABLES = (
    "CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT NOT NULL, "
    "password_hash TEXT NOT NULL, created_at DATETIME, updated_at DATETIME)",
    "CREATE TABLE user_interests (id INTEGER PRIMARY KEY AUTOINCREMENT, "
    "user_id INTEGER NOT NULL, text TEXT NOT NULL, "
    "created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at DATETIME)",
    "CREATE TABLE sources (id INTEGER PRIMARY KEY AUTOINCREMENT, type TEXT NOT NULL, "
    "name TEXT NOT NULL, canonical_url TEXT NOT NULL, external_id TEXT, "
    "last_synced_at DATETIME, created_at DATETIME)",
    "CREATE TABLE user_sources (id INTEGER PRIMARY KEY AUTOINCREMENT, "
    "user_id INTEGER NOT NULL, source_id INTEGER NOT NULL, enabled BOOLEAN NOT NULL, "
    "created_at DATETIME, UNIQUE (user_id, source_id))",
    "CREATE TABLE feed_items (id INTEGER PRIMARY KEY AUTOINCREMENT, "
    "source_id INTEGER NOT NULL, external_id TEXT NOT NULL, title TEXT, "
    "body_text TEXT NOT NULL, original_url TEXT NOT NULL, author TEXT, "
    "published_at DATETIME, collected_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, "
    "raw_payload TEXT, extracted_entities TEXT, content_hash TEXT, summary TEXT, "
    "importance_score REAL, ai_metadata TEXT, "
    "processing_status TEXT NOT NULL DEFAULT 'pending', "
    "created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at DATETIME)",
    "CREATE TABLE content_clusters (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, "
    "summary TEXT, importance_score REAL, ai_metadata TEXT, created_at DATETIME)",
    "CREATE TABLE cluster_items (id INTEGER PRIMARY KEY AUTOINCREMENT, "
    "cluster_id INTEGER NOT NULL, feed_item_id INTEGER NOT NULL, "
    "is_primary BOOLEAN NOT NULL, UNIQUE (cluster_id, feed_item_id))",
    "CREATE TABLE user_item_scores (id INTEGER PRIMARY KEY AUTOINCREMENT, "
    "user_id INTEGER NOT NULL, feed_item_id INTEGER, cluster_id INTEGER, "
    "relevance_score REAL, rank_score REAL, reason TEXT, "
    "calculated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, analysis_metadata TEXT)",
)


@pytest.fixture
def relevance_db(monkeypatch: pytest.MonkeyPatch):
    """SQLite-БД для сквозной проверки хуков и фоновых задач без брокера."""
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    with engine.begin() as connection:
        for statement in _RELEVANCE_TABLES:
            connection.execute(text(statement))
    monkeypatch.setattr("app.services.user_relevance.get_engine", lambda: engine)
    yield sessionmaker(bind=engine)
    engine.dispose()


def _seed_units_with_summaries(factory) -> tuple[int, int, int, int]:
    """Пользователь с интересом и источником, две публикации с актуальной выжимкой."""
    with factory() as session:
        user = User(email="hook@example.test", password_hash="test-only")
        source = Source(
            type=SourceType.TELEGRAM,
            name="Источник",
            external_id="hook-source",
            canonical_url="https://example.test/hook-source",
        )
        session.add_all([user, source])
        session.flush()
        session.add(UserSource(user_id=user.id, source_id=source.id, enabled=True))
        session.add(UserInterest(user_id=user.id, text="ИИ"))
        items = [
            FeedItem(
                source_id=source.id,
                external_id=f"hook-item-{index}",
                title=f"Новость {index}",
                body_text=f"Факты {index}",
                original_url=f"https://example.test/hook-item-{index}",
            )
            for index in range(2)
        ]
        session.add_all(items)
        session.flush()
        for item in items:
            result = _summary(f"Выжимка {item.id}")
            unit = content_unit_from_feed_item(item, source.name)
            item.summary = result.summary
            item.ai_metadata = FeedItemAIMetadata(
                summary=SummaryResultHistory(
                    last_good=StoredSummaryResult(
                        status="current",
                        input_fingerprint=content_input_fingerprint(unit),
                        result=result,
                    )
                )
            ).model_dump(mode="json", exclude_none=True)
        ids = (user.id, source.id, items[0].id, items[1].id)
        session.commit()
        return ids


def test_hooks_and_background_tasks_share_one_viewed_provider_resolver(
    relevance_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    factory = relevance_db
    user_id, source_id, viewed_item_id, unviewed_item_id = _seed_units_with_summaries(factory)
    recorder = _RecordingSendTask()
    monkeypatch.setattr("app.workers.scheduler.celery_app", recorder)
    provider = _Provider({("feed_item", viewed_item_id): True})
    monkeypatch.setattr(
        "app.services.viewed_status.get_viewed_status_provider", lambda: provider
    )

    assert notify_interests_changed(user_id) is True
    assert notify_source_activated(user_id, source_id) is True
    assert recorder.sent == [
        ("relevance.recalculate_user", {"user_id": user_id}),
        ("relevance.recalculate_user_source", {"user_id": user_id, "source_id": source_id}),
    ]

    from app.workers import tasks

    by_user = tasks.recalculate_user_task(user_id)
    by_source = tasks.recalculate_user_source_task(user_id, source_id)

    expected = [{"kind": "feed_item", "unit_id": unviewed_item_id}]
    assert by_user == expected
    assert by_source == expected
    scored = [
        kwargs["unit_id"]
        for name, kwargs in recorder.sent
        if name == "relevance.score_user_content_unit"
    ]
    assert scored == [unviewed_item_id, unviewed_item_id]
    assert viewed_item_id not in scored
