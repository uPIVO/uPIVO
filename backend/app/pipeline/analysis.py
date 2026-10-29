"""Общий AI-этап конвейера: детерминированный обход единиц контента.

Этап идёт после commit нормализации и склейки и обрабатывает общую БД, а не
только источники пользователя из ``--user``. Собственная логика выжимки,
важности, отпечатков и хранения не дублируется: используются
``ContentUnitAnalysisUseCase`` с явным ``kind``, ``SummarizationService`` и
``ImportanceService``.

Порядок действий для одной единицы:

1. операция ``summary``, затем ``importance`` — по отдельности и ``force=False``;
   сетевые вызовы выполняются в коротких транзакциях самого use case, без
   открытой транзакции этапа;
2. ровно одна постановка ``score_content_unit_for_eligible_users_task.delay``;
3. подтверждение ``processed`` через
   ``ContentUnitAnalysisRepository.mark_unit_processed``.

Постановка в очередь выполняется до подтверждения статуса: если брокер
недоступен, участники остаются в ``normalized``, а следующий запуск повторит
постановку без новых платных вызовов, потому что актуальные результаты
переиспользуются штатными правилами сервисов. Доставка задачи в Celery не
гарантируется, а ``processed`` не означает завершённую персональную оценку.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass
from typing import Any, Literal

from sqlalchemy import case, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.ai.analysis_metadata import summary_lineage_fingerprint
from app.ai.base import LLMError
from app.ai.dto import ImportanceInput, ImportanceResult, SummaryResult
from app.ai.importance import ImportanceService
from app.ai.repository import (
    ContentUnitAnalysisRepository,
    ContentUnitAnalysisSnapshot,
)
from app.ai.service import SummarizationService
from app.models.content_cluster import ClusterItem, ContentCluster
from app.models.enums import ProcessingStatus
from app.models.feed_item import FeedItem
from app.services.analyze_feed_item import (
    AnalysisOperation,
    AnalysisRequestError,
    AnalysisStatus,
    AnalysisStepResult,
    AnalysisStorageError,
    ContentUnitAnalysisUseCase,
)

logger = logging.getLogger(__name__)

DEFAULT_AI_LIMIT = 50
UnitKind = Literal["feed_item", "cluster"]
SessionFactory = Callable[[], Session]
Dispatch = Callable[[str, int], Any]

_ANALYZABLE_STATUSES = (ProcessingStatus.NORMALIZED, ProcessingStatus.PROCESSED)


@dataclass(frozen=True, slots=True)
class AnalysisStats:
    """Итоги общего AI-этапа за один запуск.

    ``failed`` считает единицы, которые не дошли до ``processed``;
    ``summary_failed`` и ``importance_failed`` уточняют стадию. ``queue_*``
    относится только к постановке в брокер и не означает готовую персональную
    оценку; ``receipt_failed`` — сообщение принято, но квитанция не записана.
    """

    selected: int = 0
    succeeded: int = 0
    reused: int = 0
    failed: int = 0
    skipped: int = 0
    summary_failed: int = 0
    importance_failed: int = 0
    queue_succeeded: int = 0
    queue_failed: int = 0
    receipt_failed: int = 0

    @property
    def has_failures(self) -> bool:
        return self.failed > 0 or self.queue_failed > 0


@dataclass(frozen=True, slots=True)
class _Candidate:
    """Кандидат глобального обхода и его текущая готовность по статусам."""

    kind: UnitKind
    unit_id: int
    needs_finalize: bool
    usable: bool = True


@dataclass(frozen=True, slots=True)
class _SelectedUnit:
    """Отобранная единица вместе с наблюдаемым снимком анализа."""

    kind: UnitKind
    unit_id: int
    input_fingerprint: str
    summary: SummaryResult | None
    importance: ImportanceResult | None
    needs_analysis: bool


@dataclass(slots=True)
class _Counters:
    selected: int = 0
    succeeded: int = 0
    reused: int = 0
    failed: int = 0
    skipped: int = 0
    summary_failed: int = 0
    importance_failed: int = 0
    queue_succeeded: int = 0
    queue_failed: int = 0
    receipt_failed: int = 0

    def build(self) -> AnalysisStats:
        return AnalysisStats(**asdict(self))


async def run_common_analysis(
    *,
    session_factory: SessionFactory,
    ai_limit: int | None = DEFAULT_AI_LIMIT,
    summarization_service: SummarizationService | None = None,
    importance_service: ImportanceService | None = None,
    repository: ContentUnitAnalysisRepository | None = None,
    dispatch: Dispatch | None = None,
) -> AnalysisStats:
    """Выполнить общий AI-этап по текущему состоянию общей БД.

    ``ai_limit`` ограничивает количество единиц за запуск; ``None`` — явный
    полный проход по всему backlog с соответствующей стоимостью платных
    вызовов. Единицы, не требующие ни анализа, ни подтверждения статуса, лимит
    не занимают.
    """
    if ai_limit is not None and ai_limit < 1:
        raise ValueError("ai_limit must be >= 1")

    analysis_repository = repository if repository is not None else ContentUnitAnalysisRepository()
    summarization = (
        summarization_service
        if summarization_service is not None
        else _default_summarization_service()
    )
    importance = (
        importance_service if importance_service is not None else _default_importance_service()
    )
    send = dispatch if dispatch is not None else _dispatch_relevance

    units, invalid = _select_units(
        session_factory=session_factory,
        repository=analysis_repository,
        summarization=summarization,
        importance=importance,
        limit=ai_limit,
    )
    counters = _Counters(selected=len(units), skipped=invalid)
    for unit in units:
        await _process_unit(
            unit,
            counters=counters,
            session_factory=session_factory,
            repository=analysis_repository,
            summarization=summarization,
            importance=importance,
            dispatch=send,
        )

    stats = counters.build()
    logger.info(
        "AI-этап: selected=%s succeeded=%s reused=%s failed=%s skipped=%s "
        "summary_failed=%s importance_failed=%s queue_succeeded=%s queue_failed=%s "
        "receipt_failed=%s",
        stats.selected,
        stats.succeeded,
        stats.reused,
        stats.failed,
        stats.skipped,
        stats.summary_failed,
        stats.importance_failed,
        stats.queue_succeeded,
        stats.queue_failed,
        stats.receipt_failed,
    )
    return stats


def _select_units(
    *,
    session_factory: SessionFactory,
    repository: ContentUnitAnalysisRepository,
    summarization: SummarizationService,
    importance: ImportanceService,
    limit: int | None,
) -> tuple[list[_SelectedUnit], int]:
    """Детерминированно отобрать единицы, которые требуют работы.

    Единица нужна, если требуется анализ (нет актуальных результатов или
    сменилась конфигурация провайдера), не все участники ``processed`` или нет
    актуальной квитанции постановки. Готовые единицы с актуальной квитанцией не
    попадают в выборку и не занимают лимит.
    """
    selected: list[_SelectedUnit] = []
    invalid = 0
    with session_factory() as session:
        for candidate in _iter_candidates(session):
            if not candidate.usable:
                invalid += 1
                continue
            try:
                snapshot = repository.load_snapshot(
                    session, kind=candidate.kind, unit_id=candidate.unit_id
                )
            except ValueError:
                # Состав изменился между выборкой и чтением единицы.
                invalid += 1
                continue
            if snapshot is None:
                invalid += 1
                continue
            needs_analysis = _needs_analysis(snapshot, summarization, importance)
            if not (needs_analysis or candidate.needs_finalize or _needs_dispatch(snapshot)):
                continue
            selected.append(
                _SelectedUnit(
                    kind=candidate.kind,
                    unit_id=candidate.unit_id,
                    input_fingerprint=snapshot.input_fingerprint,
                    summary=snapshot.summary_result,
                    importance=snapshot.importance_result,
                    needs_analysis=needs_analysis,
                )
            )
            if limit is not None and len(selected) >= limit:
                break
    return selected, invalid


def _iter_candidates(session: Session) -> Iterator[_Candidate]:
    """Глобальный обход: сначала кластеры, затем одиночные публикации, по id."""
    clusters = session.execute(
        select(
            ContentCluster.id,
            func.max(case((FeedItem.processing_status.notin_(_ANALYZABLE_STATUSES), 1), else_=0)),
            func.max(case((FeedItem.processing_status != ProcessingStatus.PROCESSED, 1), else_=0)),
        )
        .join(ClusterItem, ClusterItem.cluster_id == ContentCluster.id)
        .join(FeedItem, FeedItem.id == ClusterItem.feed_item_id)
        .group_by(ContentCluster.id)
        .having(func.count(FeedItem.id) >= 2)
        .order_by(ContentCluster.id)
    ).all()
    for cluster_id, has_invalid, has_unfinished in clusters:
        yield _Candidate(
            kind="cluster",
            unit_id=cluster_id,
            needs_finalize=bool(has_unfinished),
            usable=not has_invalid,
        )

    member_cluster = select(ClusterItem.cluster_id).where(ClusterItem.feed_item_id == FeedItem.id)
    items = session.execute(
        select(FeedItem.id, FeedItem.processing_status)
        .where(
            FeedItem.processing_status.in_(_ANALYZABLE_STATUSES),
            ~member_cluster.exists(),
        )
        .order_by(FeedItem.id)
    ).all()
    for item_id, status in items:
        yield _Candidate(
            kind="feed_item",
            unit_id=item_id,
            needs_finalize=status != ProcessingStatus.PROCESSED,
        )


def _needs_analysis(
    snapshot: ContentUnitAnalysisSnapshot,
    summarization: SummarizationService,
    importance: ImportanceService,
) -> bool:
    """Нужен ли единице общий анализ по штатным правилам переиспользования.

    Актуальность подтверждают снимок репозитория и отпечатки обоих провайдеров, а
    не только ``processing_status`` или непустая выжимка. Вход важности строится
    из текущей выжимки тем же контрактом, что использует use case.
    """
    summary = snapshot.summary_result
    if summary is None or snapshot.importance_result is None:
        return True
    if summarization.requires_provider_call(snapshot.unit, prior_result=summary, force=False):
        return True
    return importance.requires_provider_call(
        ImportanceInput(summary=summary.summary),
        prior_result=snapshot.importance_result,
        force=False,
    )


def _needs_dispatch(snapshot: ContentUnitAnalysisSnapshot) -> bool:
    """Нужна ли повторная постановка: квитанции нет или она устарела."""
    receipt = snapshot.relevance_dispatch
    summary = snapshot.summary_result
    if receipt is None or summary is None:
        return True
    return (
        receipt.input_fingerprint != snapshot.input_fingerprint
        or receipt.summary_lineage_fingerprint != summary_lineage_fingerprint(summary)
    )


async def _process_unit(
    unit: _SelectedUnit,
    *,
    counters: _Counters,
    session_factory: SessionFactory,
    repository: ContentUnitAnalysisRepository,
    summarization: SummarizationService,
    importance: ImportanceService,
    dispatch: Dispatch,
) -> None:
    summary = unit.summary
    importance_result = unit.importance
    reused = True

    if unit.needs_analysis:
        use_case = ContentUnitAnalysisUseCase(
            session_factory=session_factory,
            kind=unit.kind,
            repository=repository,
            summarization_service=summarization,
            importance_service=importance,
        )
        summary_step = await _request_step(use_case, unit, AnalysisOperation.SUMMARY, counters)
        if summary_step is None:
            return
        importance_step = await _request_step(
            use_case, unit, AnalysisOperation.IMPORTANCE, counters
        )
        if importance_step is None:
            return
        if not isinstance(summary_step.result, SummaryResult) or not isinstance(
            importance_step.result, ImportanceResult
        ):
            counters.failed += 1
            _log_unit(unit, "нет результата", AnalysisStatus.FAILED)
            return
        summary = summary_step.result
        importance_result = importance_step.result
        reused = bool(summary_step.reused) and bool(importance_step.reused)

    if summary is None or importance_result is None:
        counters.failed += 1
        _log_unit(unit, "нет актуальных результатов", AnalysisStatus.FAILED)
        return

    # 1. Подтверждение `processed` и commit — до постановки в брокер.
    marked = _confirm_processed(
        unit,
        summary=summary,
        importance=importance_result,
        session_factory=session_factory,
        repository=repository,
    )
    if not marked:
        counters.failed += 1
        _log_unit(unit, "изменилась до подтверждения", AnalysisStatus.STALE_CANDIDATE)
        return

    # 2. Ровно одна постановка на единицу.
    try:
        dispatch(unit.kind, unit.unit_id)
    except Exception:
        # Результаты и `processed` остаются; квитанции нет, поэтому следующий
        # запуск повторит только постановку без платных вызовов.
        counters.failed += 1
        counters.queue_failed += 1
        logger.warning(
            "AI-этап: постановка персональной оценки не удалась kind=%s unit_id=%s",
            unit.kind,
            unit.unit_id,
        )
        return
    counters.queue_succeeded += 1

    # 3. Квитанция успешной постановки в отдельной короткой транзакции.
    receipted = _record_receipt(
        unit,
        summary=summary,
        session_factory=session_factory,
        repository=repository,
    )
    if not receipted:
        counters.failed += 1
        counters.receipt_failed += 1
        logger.warning(
            "AI-этап: квитанция постановки не записана kind=%s unit_id=%s",
            unit.kind,
            unit.unit_id,
        )
        return

    counters.succeeded += 1
    if reused:
        counters.reused += 1


def _confirm_processed(
    unit: _SelectedUnit,
    *,
    summary: SummaryResult,
    importance: ImportanceResult,
    session_factory: SessionFactory,
    repository: ContentUnitAnalysisRepository,
) -> bool:
    """Атомарно подтвердить `processed`, если результаты всё ещё актуальны."""
    try:
        with session_factory() as session:
            with session.begin():
                return repository.mark_unit_processed(
                    session,
                    kind=unit.kind,
                    unit_id=unit.unit_id,
                    expected_input_fingerprint=unit.input_fingerprint,
                    expected_summary=summary,
                    expected_importance=importance,
                )
    except (ValueError, SQLAlchemyError):
        return False


def _record_receipt(
    unit: _SelectedUnit,
    *,
    summary: SummaryResult,
    session_factory: SessionFactory,
    repository: ContentUnitAnalysisRepository,
) -> bool:
    """Записать квитанцию постановки; ошибка оставляет единицу повторяемой."""
    try:
        with session_factory() as session:
            with session.begin():
                return repository.mark_relevance_dispatched(
                    session,
                    kind=unit.kind,
                    unit_id=unit.unit_id,
                    expected_input_fingerprint=unit.input_fingerprint,
                    expected_summary_lineage_fingerprint=summary_lineage_fingerprint(summary),
                )
    except (ValueError, SQLAlchemyError):
        return False


async def _request_step(
    use_case: ContentUnitAnalysisUseCase,
    unit: _SelectedUnit,
    operation: AnalysisOperation,
    counters: _Counters,
) -> AnalysisStepResult | None:
    """Выполнить одну операцию use case и учесть её результат в статистике."""
    try:
        outcome = await use_case.run(unit.unit_id, operation=operation, force=False)
    except (AnalysisRequestError, AnalysisStorageError, LLMError):
        _count_failure(counters, operation)
        _log_unit(unit, "ошибка запроса", AnalysisStatus.FAILED, operation)
        return None

    step = outcome.summary if operation is AnalysisOperation.SUMMARY else outcome.importance
    if step is not None and step.status is AnalysisStatus.CURRENT:
        return step
    if step is None or step.status in (AnalysisStatus.NOT_FOUND, AnalysisStatus.SKIPPED):
        # Единица уже выбрана к обработке, поэтому неподтверждённый результат
        # считается неуспехом стадии, а не ожидаемым пропуском при отборе.
        status = step.status if step is not None else AnalysisStatus.FAILED
        counters.skipped += 1
        _count_failure(counters, operation)
        _log_unit(unit, "пропущена", status, operation)
        return None
    _count_failure(counters, operation)
    _log_unit(unit, "неподтверждённый результат", step.status, operation)
    return None


def _count_failure(counters: _Counters, operation: AnalysisOperation) -> None:
    counters.failed += 1
    if operation is AnalysisOperation.SUMMARY:
        counters.summary_failed += 1
    else:
        counters.importance_failed += 1


def _log_unit(
    unit: _SelectedUnit,
    category: str,
    status: AnalysisStatus,
    operation: AnalysisOperation | None = None,
) -> None:
    """Безопасный лог: идентификаторы, операция, статус и категория без текстов."""
    logger.warning(
        "AI-этап: единица не завершена kind=%s unit_id=%s операция=%s статус=%s категория=%s",
        unit.kind,
        unit.unit_id,
        operation.value if operation is not None else "-",
        status.value,
        category,
    )


def _default_summarization_service() -> SummarizationService:
    from app.ai.openrouter import OpenRouterAdapter
    from app.core.config import get_settings

    return SummarizationService(OpenRouterAdapter(get_settings()))


def _default_importance_service() -> ImportanceService:
    from app.ai.jev import JevImportanceAdapter
    from app.core.config import get_settings

    return ImportanceService(JevImportanceAdapter(get_settings()))


def _dispatch_relevance(kind: str, unit_id: int) -> Any:
    """Ровно одна готовая точка входа ITD-86 на единицу контента."""
    from app.workers.tasks import score_content_unit_for_eligible_users_task

    return score_content_unit_for_eligible_users_task.delay(kind, unit_id)
