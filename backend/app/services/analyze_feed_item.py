from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal

from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.ai.analysis_metadata import interests_input_fingerprint, summary_lineage_fingerprint
from app.ai.base import LLMError
from app.ai.dto import (
    ImportanceResult,
    RelevanceInput,
    RelevanceResult,
    SummaryResult,
)
from app.ai.importance import ImportanceService
from app.ai.repository import (
    ContentUnitAnalysisRepository,
    ContentUnitAnalysisSnapshot,
    FeedItemInClusterError,
    SaveDisposition,
)
from app.ai.service import RelevanceService, SummarizationService
from app.models.user import User

SessionFactory = Callable[[], Session]
AnalysisResult = SummaryResult | ImportanceResult | RelevanceResult


class AnalysisOperation(StrEnum):
    SUMMARY = "summary"
    IMPORTANCE = "importance"
    RELEVANCE = "relevance"
    ALL = "all"

    @property
    def includes_summary(self) -> bool:
        return self in {AnalysisOperation.SUMMARY, AnalysisOperation.ALL}

    @property
    def includes_importance(self) -> bool:
        return self in {AnalysisOperation.IMPORTANCE, AnalysisOperation.ALL}

    @property
    def includes_relevance(self) -> bool:
        return self in {AnalysisOperation.RELEVANCE, AnalysisOperation.ALL}

    @property
    def requires_user_context(self) -> bool:
        return self.includes_relevance


class AnalysisStatus(StrEnum):
    CURRENT = "current"
    STALE_CANDIDATE = "stale_candidate"
    FAILED = "failed"
    SKIPPED = "skipped"
    NOT_FOUND = "not_found"


class AnalysisRequestError(RuntimeError):
    pass


class AnalysisStorageError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class AnalysisStepResult:
    status: AnalysisStatus
    reused: bool | None = None
    save_disposition: SaveDisposition | None = None
    result: AnalysisResult | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class ContentUnitAnalysisOutcome:
    unit_id: int
    operation: AnalysisOperation
    attempt_count: int
    kind: Literal["feed_item", "cluster"] = "feed_item"
    summary: AnalysisStepResult | None = None
    importance: AnalysisStepResult | None = None
    relevance: AnalysisStepResult | None = None

    @property
    def feed_item_id(self) -> int | None:
        return self.unit_id if self.kind == "feed_item" else None

    @property
    def succeeded(self) -> bool:
        steps = [
            step for step in (self.summary, self.importance, self.relevance) if step is not None
        ]
        return bool(steps) and all(step.status is AnalysisStatus.CURRENT for step in steps)

    @property
    def status(self) -> str:
        if self.succeeded:
            return AnalysisStatus.CURRENT.value
        steps = [
            step for step in (self.summary, self.importance, self.relevance) if step is not None
        ]
        if any(step.status is AnalysisStatus.STALE_CANDIDATE for step in steps):
            return AnalysisStatus.STALE_CANDIDATE.value
        return "partial"


FeedItemAnalysisOutcome = ContentUnitAnalysisOutcome


class ContentUnitAnalysisUseCase:
    def __init__(
        self,
        *,
        session_factory: SessionFactory,
        kind: Literal["feed_item", "cluster"] = "feed_item",
        repository: ContentUnitAnalysisRepository | None = None,
        summarization_service: SummarizationService | None = None,
        importance_service: ImportanceService | None = None,
        relevance_service: RelevanceService | None = None,
        max_attempts: int = 2,
    ) -> None:
        """Собрать сценарий анализа одной единицы контента.

        `max_attempts` ограничивает внутренние повторы при устаревшем результате
        обработки контента. Путь с интересами из БД передаёт 1: он сам перечитывает
        актуальный список интересов и повторяет оценку, не тратя лишний AI-вызов.
        """
        if kind not in ("feed_item", "cluster"):
            raise ValueError("kind должен иметь значение 'feed_item' или 'cluster'.")
        if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or max_attempts < 1:
            raise ValueError("Количество попыток анализа должно быть положительным целым числом.")

        self._session_factory = session_factory
        self._repository = repository if repository is not None else ContentUnitAnalysisRepository()
        self._kind = kind
        self._summarization_service = summarization_service
        self._importance_service = importance_service
        self._relevance_service = relevance_service
        self._max_attempts = max_attempts

    async def run(
        self,
        unit_id: int,
        *,
        operation: AnalysisOperation | str = AnalysisOperation.ALL,
        user_id: int | None = None,
        interests: list[str] | None = None,
        force: bool = False,
    ) -> ContentUnitAnalysisOutcome:
        analysis_operation = self._validate_operation(operation)
        self._validate_unit_id(unit_id)
        validated_interests = self._validate_context(analysis_operation, user_id, interests)
        self._require_services(analysis_operation)

        for attempt in range(1, self._max_attempts + 1):
            if analysis_operation.requires_user_context:
                self._ensure_user(user_id)

            snapshot = self._load_snapshot(unit_id)
            if snapshot is None:
                item_name = "Элемент ленты" if self._kind == "feed_item" else "Кластер"
                raise AnalysisRequestError(f"{item_name} не найден.")

            if (
                analysis_operation.includes_summary
                and self._summarization_service.requires_provider_call(
                    snapshot.unit,
                    prior_result=snapshot.summary_result,
                    force=force,
                )
            ):
                self._validate_service_configuration(analysis_operation)

            if not analysis_operation.includes_summary and snapshot.summary_result is None:
                if attempt == 1:
                    raise AnalysisRequestError(
                        "Нет актуальной выжимки. Сначала выполните операцию summary."
                    )
                return self._missing_summary_outcome(unit_id, analysis_operation, attempt)

            summary_step: AnalysisStepResult | None = None
            importance_step: AnalysisStepResult | None = None
            relevance_step: AnalysisStepResult | None = None
            summary_result = snapshot.summary_result
            stale_candidate_saved = False
            operation_failed = False

            if analysis_operation.includes_summary:
                summary_step, summary_result = await self._summarize_and_save(
                    unit_id,
                    snapshot,
                    force=force,
                )
                if summary_step.status in {AnalysisStatus.FAILED, AnalysisStatus.NOT_FOUND}:
                    return self._outcome(
                        unit_id,
                        analysis_operation,
                        attempt,
                        summary=summary_step,
                        importance=self._skipped_step(analysis_operation.includes_importance),
                        relevance=self._skipped_step(analysis_operation.includes_relevance),
                    )
                if summary_step.status is AnalysisStatus.STALE_CANDIDATE:
                    if attempt < self._max_attempts:
                        continue
                    return self._outcome(
                        unit_id,
                        analysis_operation,
                        attempt,
                        summary=summary_step,
                        importance=self._skipped_step(analysis_operation.includes_importance),
                        relevance=self._skipped_step(analysis_operation.includes_relevance),
                    )
                if analysis_operation is AnalysisOperation.SUMMARY:
                    return self._outcome(unit_id, analysis_operation, attempt, summary=summary_step)

            if summary_result is None:
                raise AnalysisRequestError(
                    "Нет актуальной выжимки. Сначала выполните операцию summary."
                )

            summary_lineage = summary_lineage_fingerprint(summary_result)
            summary_is_current = not analysis_operation.includes_summary or (
                summary_step is not None
                and summary_step.save_disposition is SaveDisposition.CURRENT
            )

            if analysis_operation.includes_importance:
                importance_step, failed, stale = await self._score_importance(
                    unit_id,
                    snapshot,
                    summary_result,
                    summary_lineage,
                    force=force,
                    summary_is_current=summary_is_current,
                )
                operation_failed = operation_failed or failed
                if stale:
                    if attempt < self._max_attempts and not operation_failed:
                        continue
                    return self._outcome(
                        unit_id,
                        analysis_operation,
                        attempt,
                        summary=summary_step,
                        importance=importance_step,
                        relevance=self._skipped_step(analysis_operation.includes_relevance),
                    )

            if analysis_operation.includes_relevance:
                relevance_step, failed, stale = await self._score_relevance(
                    unit_id,
                    snapshot,
                    summary_result,
                    summary_lineage,
                    user_id=user_id,
                    interests=validated_interests,
                    force=force,
                )
                operation_failed = operation_failed or failed
                stale_candidate_saved = stale_candidate_saved or stale

            if stale_candidate_saved and attempt < self._max_attempts and not operation_failed:
                continue
            return self._outcome(
                unit_id,
                analysis_operation,
                attempt,
                summary=summary_step,
                importance=importance_step,
                relevance=relevance_step,
            )

        raise AssertionError("Повтор анализа вышел за ограниченный цикл.")

    @staticmethod
    def _validate_operation(operation: AnalysisOperation | str) -> AnalysisOperation:
        try:
            return AnalysisOperation(operation)
        except ValueError:
            raise AnalysisRequestError("Неизвестная операция анализа.") from None

    @staticmethod
    def _validate_unit_id(unit_id: int) -> None:
        if isinstance(unit_id, bool) or not isinstance(unit_id, int) or unit_id <= 0:
            raise AnalysisRequestError(
                "Идентификатор единицы анализа должен быть положительным числом."
            )

    @staticmethod
    def _validate_context(
        operation: AnalysisOperation,
        user_id: int | None,
        interests: list[str] | None,
    ) -> list[str]:
        if not operation.requires_user_context:
            return []
        if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
            raise AnalysisRequestError("Для оценки релевантности укажите положительный user_id.")
        if interests is None:
            raise AnalysisRequestError("Для оценки релевантности укажите список интересов.")
        try:
            validated = RelevanceInput.model_validate(
                {"summary": "Проверочная выжимка", "interests": interests}
            )
        except ValidationError:
            raise AnalysisRequestError("Некорректный список интересов.") from None
        return list(validated.interests)

    def _require_services(self, operation: AnalysisOperation) -> None:
        if operation.includes_summary and self._summarization_service is None:
            raise AnalysisRequestError("Не настроен сервис суммаризации.")
        if operation.includes_importance and self._importance_service is None:
            raise AnalysisRequestError("Не настроен сервис оценки общей важности.")
        if operation.includes_relevance and self._relevance_service is None:
            raise AnalysisRequestError("Не настроен сервис оценки релевантности.")

    def _validate_service_configuration(self, operation: AnalysisOperation) -> None:
        services = []
        if operation.includes_summary:
            services.append(self._summarization_service)
        if operation.includes_importance:
            services.append(self._importance_service)
        if operation.includes_relevance:
            services.append(self._relevance_service)
        for service in services:
            try:
                service.validate_configuration()
            except LLMError:
                raise
            except Exception:
                raise LLMError("Некорректная конфигурация сервиса анализа.") from None

    def _ensure_user(self, user_id: int | None) -> None:
        if user_id is None:
            raise AnalysisRequestError("Для оценки релевантности укажите user_id.")

        def get_user(session: Session) -> bool:
            return session.get(User, user_id) is not None

        if not self._transaction(get_user, "Не удалось проверить пользователя"):
            raise AnalysisRequestError("Пользователь не найден.")

    def _load_snapshot(self, unit_id: int) -> ContentUnitAnalysisSnapshot | None:
        item_name = "элемент ленты" if self._kind == "feed_item" else "кластер"
        return self._transaction(
            lambda session: self._repository.load_snapshot(
                session, kind=self._kind, unit_id=unit_id
            ),
            f"Не удалось прочитать {item_name}",
        )

    def _transaction(self, action: Callable[[Session], Any], failure: str) -> Any:
        try:
            with self._session_factory() as session:
                with session.begin():
                    return action(session)
        except FeedItemInClusterError as error:
            raise AnalysisRequestError(str(error)) from None
        except Exception:
            raise AnalysisStorageError(f"{failure} из базы данных.") from None

    def _save_summary(
        self, unit_id: int, input_fingerprint: str, result: SummaryResult
    ) -> SaveDisposition:
        return self._transaction(
            lambda session: self._repository.save_summary(
                session,
                kind=self._kind,
                unit_id=unit_id,
                expected_input_fingerprint=input_fingerprint,
                result=result,
            ),
            "Не удалось сохранить выжимку",
        )

    def _save_importance(
        self,
        unit_id: int,
        snapshot: ContentUnitAnalysisSnapshot,
        summary_lineage: str,
        result: ImportanceResult,
    ) -> SaveDisposition:
        return self._transaction(
            lambda session: self._repository.save_importance(
                session,
                kind=self._kind,
                unit_id=unit_id,
                expected_input_fingerprint=snapshot.input_fingerprint,
                expected_summary_lineage_fingerprint=summary_lineage,
                result=result,
            ),
            "Не удалось сохранить оценку общей важности",
        )

    def _save_relevance(
        self,
        unit_id: int,
        snapshot: ContentUnitAnalysisSnapshot,
        summary_lineage: str,
        interests: list[str],
        user_id: int,
        result: RelevanceResult,
    ) -> SaveDisposition:
        return self._transaction(
            lambda session: self._repository.save_relevance(
                session,
                user_id=user_id,
                kind=self._kind,
                unit_id=unit_id,
                source_input_fingerprint=snapshot.input_fingerprint,
                expected_summary_lineage_fingerprint=summary_lineage,
                interests_fingerprint=interests_input_fingerprint(interests),
                result=result,
            ),
            "Не удалось сохранить оценку релевантности",
        )

    async def _summarize_and_save(
        self,
        unit_id: int,
        snapshot: ContentUnitAnalysisSnapshot,
        *,
        force: bool,
    ) -> tuple[AnalysisStepResult, SummaryResult | None]:
        try:
            run = await self._summarization_service.summarize(
                snapshot.unit,
                prior_result=snapshot.summary_result,
                force=force,
            )
        except LLMError as error:
            return self._failed_step(str(error)), None
        except Exception:
            return self._failed_step("Не удалось получить выжимку."), None

        try:
            disposition = self._save_summary(unit_id, snapshot.input_fingerprint, run.result)
        except AnalysisStorageError as error:
            return self._failed_step(str(error)), None
        return self._saved_step(disposition, run.reused, run.result), run.result

    async def _score_importance(
        self,
        unit_id: int,
        snapshot: ContentUnitAnalysisSnapshot,
        summary_result: SummaryResult,
        summary_lineage: str,
        *,
        force: bool,
        summary_is_current: bool,
    ) -> tuple[AnalysisStepResult, bool, bool]:
        prior_result = None
        if (
            summary_is_current
            and snapshot.summary_result is not None
            and summary_lineage_fingerprint(snapshot.summary_result) == summary_lineage
        ):
            prior_result = snapshot.importance_result
        try:
            run = await self._importance_service.score_importance(
                {"summary": summary_result.summary},
                prior_result=prior_result,
                force=force,
            )
        except LLMError as error:
            return self._failed_step(str(error)), True, False
        except Exception:
            return self._failed_step("Не удалось получить оценку общей важности."), True, False

        try:
            disposition = self._save_importance(unit_id, snapshot, summary_lineage, run.result)
        except AnalysisStorageError as error:
            return self._failed_step(str(error)), True, False
        step = self._saved_step(disposition, run.reused, run.result)
        return (
            step,
            disposition is SaveDisposition.NOT_FOUND,
            disposition is SaveDisposition.STALE_CANDIDATE,
        )

    async def _score_relevance(
        self,
        unit_id: int,
        snapshot: ContentUnitAnalysisSnapshot,
        summary_result: SummaryResult,
        summary_lineage: str,
        *,
        user_id: int | None,
        interests: list[str],
        force: bool,
    ) -> tuple[AnalysisStepResult, bool, bool]:
        if user_id is None:
            return self._failed_step("Для оценки релевантности укажите user_id."), True, False
        interests_fingerprint = interests_input_fingerprint(interests)
        try:
            prior_result = self._transaction(
                lambda session: self._repository.get_relevance_prior(
                    session,
                    user_id=user_id,
                    kind=self._kind,
                    unit_id=unit_id,
                    expected_summary_lineage_fingerprint=summary_lineage,
                    interests_fingerprint=interests_fingerprint,
                ),
                "Не удалось прочитать предыдущую оценку релевантности",
            )
        except AnalysisStorageError as error:
            return self._failed_step(str(error)), True, False

        try:
            run = await self._relevance_service.score_relevance(
                {"summary": summary_result.summary, "interests": interests},
                prior_result=prior_result,
                force=force,
            )
        except LLMError as error:
            return self._failed_step(str(error)), True, False
        except Exception:
            return self._failed_step("Не удалось получить оценку релевантности."), True, False

        try:
            disposition = self._save_relevance(
                unit_id,
                snapshot,
                summary_lineage,
                interests,
                user_id,
                run.result,
            )
        except AnalysisStorageError as error:
            return self._failed_step(str(error)), True, False
        step = self._saved_step(disposition, run.reused, run.result)
        return (
            step,
            disposition is SaveDisposition.NOT_FOUND,
            disposition is SaveDisposition.STALE_CANDIDATE,
        )

    @staticmethod
    def _saved_step(
        disposition: SaveDisposition,
        reused: bool,
        result: AnalysisResult,
    ) -> AnalysisStepResult:
        if disposition is SaveDisposition.CURRENT:
            status = AnalysisStatus.CURRENT
        elif disposition is SaveDisposition.STALE_CANDIDATE:
            status = AnalysisStatus.STALE_CANDIDATE
        else:
            status = AnalysisStatus.NOT_FOUND
        return AnalysisStepResult(
            status=status,
            reused=reused,
            save_disposition=disposition,
            result=result,
        )

    @staticmethod
    def _failed_step(error: str) -> AnalysisStepResult:
        return AnalysisStepResult(status=AnalysisStatus.FAILED, error=error)

    @staticmethod
    def _skipped_step(skipped: bool) -> AnalysisStepResult | None:
        if not skipped:
            return None
        return AnalysisStepResult(status=AnalysisStatus.SKIPPED)

    def _missing_summary_outcome(
        self,
        unit_id: int,
        operation: AnalysisOperation,
        attempt_count: int,
    ) -> ContentUnitAnalysisOutcome:
        message = (
            "После изменения исходных данных нет актуальной выжимки. Запустите операцию summary."
        )
        failed = AnalysisStepResult(status=AnalysisStatus.FAILED, error=message)
        return ContentUnitAnalysisOutcome(
            unit_id=unit_id,
            operation=operation,
            attempt_count=attempt_count,
            kind=self._kind,
            importance=failed if operation.includes_importance else None,
            relevance=failed if operation.includes_relevance else None,
        )

    def _outcome(
        self,
        unit_id: int,
        operation: AnalysisOperation,
        attempt_count: int,
        *,
        summary: AnalysisStepResult | None = None,
        importance: AnalysisStepResult | None = None,
        relevance: AnalysisStepResult | None = None,
    ) -> ContentUnitAnalysisOutcome:
        return ContentUnitAnalysisOutcome(
            unit_id=unit_id,
            operation=operation,
            attempt_count=attempt_count,
            kind=self._kind,
            summary=summary,
            importance=importance,
            relevance=relevance,
        )


class FeedItemAnalysisUseCase(ContentUnitAnalysisUseCase):
    def __init__(
        self,
        *,
        session_factory: SessionFactory,
        repository: ContentUnitAnalysisRepository | None = None,
        summarization_service: SummarizationService | None = None,
        importance_service: ImportanceService | None = None,
        relevance_service: RelevanceService | None = None,
        max_attempts: int = 2,
    ) -> None:
        super().__init__(
            session_factory=session_factory,
            kind="feed_item",
            repository=repository,
            summarization_service=summarization_service,
            importance_service=importance_service,
            relevance_service=relevance_service,
            max_attempts=max_attempts,
        )

    async def run(self, feed_item_id: int, **kwargs: Any) -> ContentUnitAnalysisOutcome:  # type: ignore[override]
        return await super().run(unit_id=feed_item_id, **kwargs)
