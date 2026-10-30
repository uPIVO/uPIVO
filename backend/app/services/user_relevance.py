"""DB-backed персональная оценка по интересам пользователя.

Модуль связывает уже существующие `ContentUnitAnalysisUseCase`,
`RelevanceService` и `user_item_scores` с интересами из БД:

* пустой список интересов или отсутствие актуальной выжимки — `skip` без AI;
* публикация внутри кластера оценивается как кластер, участники отдельно нет;
* контент вне включённых источников пользователя не оценивается;
* повтор с теми же входами переиспользует актуальный результат;
* изменение интересов в ходе AI-вызова не сохраняется как `current`;
* параллельные оценки одной пары «пользователь + единица» дедуплицируются
  advisory-блокировкой PostgreSQL на отдельном соединении.

Учёт просмотров здесь не реализуется: при отсутствии `ViewedStatusProvider`
массовый ретроспективный пересчёт не запускается. Автоматический запуск после
общей выжимки принадлежит ITD-85 — точка входа описана в docs/user-interests.md.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable, Iterable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from app.ai.base import LLMError
from app.ai.factory import create_relevance_adapter
from app.ai.repository import FeedItemInClusterError
from app.ai.service import RelevanceService
from app.core.config import get_settings
from app.db.session import get_engine
from app.services import viewed_status
from app.services.analyze_feed_item import (
    AnalysisOperation,
    AnalysisRequestError,
    AnalysisStatus,
    AnalysisStorageError,
    ContentUnitAnalysisUseCase,
)
from app.services.user_relevance_storage import (
    ContentUnitRef,
    InterestGuardedRepository,
    UserRelevanceCatalog,
)
from app.services.viewed_status import ViewedStatusProvider

logger = logging.getLogger(__name__)

SessionFactory = Callable[[], Session]


class UserRelevanceStatus(StrEnum):
    CURRENT = "current"
    SKIPPED = "skipped"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class UserRelevanceOutcome:
    user_id: int
    kind: str
    unit_id: int
    status: UserRelevanceStatus
    attempts: int = 1
    reused: bool | None = None
    error: str | None = None


class RelevanceTaskScheduler(Protocol):
    """Постановка фоновых работ. HTTP-запрос не выполняет AI-вызовы сам."""

    def schedule_content_unit(self, *, user_id: int, kind: str, unit_id: int) -> None: ...

    def schedule_user_recalculation(self, *, user_id: int) -> None: ...

    def schedule_source_recalculation(self, *, user_id: int, source_id: int) -> None: ...


class AdvisoryLock(Protocol):
    """Блокировка пары «пользователь + единица» вокруг AI-операции."""

    def hold(
        self, *, user_id: int, kind: str, unit_id: int
    ) -> AbstractContextManager[bool]: ...


def advisory_lock_key(*, user_id: int, kind: str, unit_id: int) -> tuple[int, int]:
    """Стабильная пара int4-ключей для `pg_advisory_lock(int4, int4)`."""
    digest = hashlib.sha256(f"{user_id}:{kind}:{unit_id}".encode()).digest()
    return (
        int.from_bytes(digest[:4], "big", signed=True),
        int.from_bytes(digest[4:8], "big", signed=True),
    )


class PostgresAdvisoryLock:
    """Дедупликация параллельных оценок через advisory-блокировку PostgreSQL.

    Блокировка берётся на отдельном соединении без открытой транзакции, поэтому
    не удерживается во время AI-ожидания транзакцией записи, и освобождается в
    `finally` даже при ошибке провайдера.
    """

    def __init__(self, *, engine_factory: Callable[[], Engine] = get_engine) -> None:
        self._engine_factory = engine_factory

    @contextmanager
    def hold(self, *, user_id: int, kind: str, unit_id: int) -> Iterator[bool]:
        engine = self._engine_factory()
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            if connection.dialect.name != "postgresql":
                # ponytail: вне PostgreSQL advisory-блокировок нет; там оценка не
                # дедуплицируется, но fingerprint-guard при сохранении остаётся.
                yield True
                return
            key = advisory_lock_key(user_id=user_id, kind=kind, unit_id=unit_id)
            params = {"first": key[0], "second": key[1]}
            acquired = bool(
                connection.execute(
                    text("SELECT pg_try_advisory_lock(:first, :second)"), params
                ).scalar()
            )
            try:
                yield acquired
            finally:
                if acquired:
                    connection.execute(
                        text("SELECT pg_advisory_unlock(:first, :second)"), params
                    )


class UnviewedCandidateSelector:
    """Правило отбора кандидатов на пересчёт: не просмотрено и есть выжимка.

    Источник признака просмотра отделён: селектор только спрашивает
    `ViewedStatusProvider` и не предполагает, как пишутся события просмотра.
    """

    def __init__(
        self,
        *,
        viewed_status_provider: ViewedStatusProvider,
        has_current_summary: Callable[[ContentUnitRef], bool],
    ) -> None:
        self._viewed_status_provider = viewed_status_provider
        self._has_current_summary = has_current_summary

    def select(
        self, user_id: int, candidates: Iterable[ContentUnitRef]
    ) -> list[ContentUnitRef]:
        selected: list[ContentUnitRef] = []
        for ref in candidates:
            if not self._has_current_summary(ref):
                continue
            if self._viewed_status_provider.was_viewed(user_id, ref.kind, ref.unit_id):
                continue
            selected.append(ref)
        return selected


class UserRelevanceService:
    """Программный контракт ITD-86 для ITD-85 и для фоновых хуков."""

    def __init__(
        self,
        *,
        session_factory: SessionFactory,
        relevance_service_factory: Callable[[], RelevanceService],
        repository: InterestGuardedRepository,
        catalog: UserRelevanceCatalog,
        scheduler: RelevanceTaskScheduler,
        viewed_status_provider: ViewedStatusProvider | None = None,
        selector: UnviewedCandidateSelector | None = None,
        lock: AdvisoryLock | None = None,
        max_attempts: int = 2,
    ) -> None:
        self._session_factory = session_factory
        self._relevance_service_factory = relevance_service_factory
        self._repository = repository
        self._catalog = catalog
        self._scheduler = scheduler
        self._viewed_status_provider = viewed_status_provider
        self._selector = selector
        self._lock = lock if lock is not None else PostgresAdvisoryLock()
        self._max_attempts = max(1, max_attempts)

    def load_user_interests(self, user_id: int) -> list[str]:
        """Полный список интересов в устойчивом порядке (по `id`)."""
        return self._catalog.load_user_interests(user_id)

    async def score_user_content_unit(
        self, user_id: int, kind: str, unit_id: int
    ) -> UserRelevanceOutcome:
        """Оценить одну единицу контента для одного пользователя.

        Пустой список интересов, отсутствие актуальной выжимки или доступа к
        включённому источнику означают `skip` без обращения к AI.
        """
        ref = self._catalog.resolve_unit(kind, unit_id)
        if ref is None:
            return self._outcome(
                user_id, kind, unit_id, UserRelevanceStatus.SKIPPED, error="Единица не найдена."
            )
        if not self._catalog.load_user_interests(user_id):
            return self._outcome(
                user_id,
                ref.kind,
                ref.unit_id,
                UserRelevanceStatus.SKIPPED,
                error="У пользователя нет интересов.",
            )
        with self._lock.hold(user_id=user_id, kind=ref.kind, unit_id=ref.unit_id) as acquired:
            if not acquired:
                return self._outcome(
                    user_id,
                    ref.kind,
                    ref.unit_id,
                    UserRelevanceStatus.SKIPPED,
                    error="Оценка этой единицы уже выполняется.",
                )
            return await self._score_locked(user_id, ref)

    def score_content_unit_for_eligible_users(self, kind: str, unit_id: int) -> list[int]:
        """Поставить фоновые оценки для пользователей с доступом и интересами.

        Возвращает id пользователей, для которых задачи уже поставлены; AI здесь
        не вызывается. Точка входа ITD-85 после готовности общей выжимки.
        """
        ref = self._catalog.resolve_unit(kind, unit_id)
        if ref is None:
            return []
        source_ids = self._catalog.unit_source_ids(ref)
        if not source_ids:
            return []
        user_ids = self._catalog.eligible_user_ids(source_ids)
        for user_id in user_ids:
            self._scheduler.schedule_content_unit(
                user_id=user_id, kind=ref.kind, unit_id=ref.unit_id
            )
        return user_ids

    def recalculate_unviewed_for_user(self, user_id: int) -> list[ContentUnitRef]:
        """Пересчитать только непросмотренные единицы включённых источников.

        Без подключённого `ViewedStatusProvider` статус неизвестен, поэтому
        исторические единицы не выбираются и не ставятся в очередь.
        """
        return self._recalculate(user_id, source_id=None)

    def recalculate_unviewed_source_for_user(
        self, user_id: int, source_id: int
    ) -> list[ContentUnitRef]:
        """То же для одного источника после его повторного включения."""
        return self._recalculate(user_id, source_id=source_id)

    def schedule_interests_recalculation(self, user_id: int) -> bool:
        """Фоновый пересчёт после фактического изменения интересов."""
        if self._viewed_status_provider is None:
            return False
        if not self._catalog.load_user_interests(user_id):
            return False
        self._scheduler.schedule_user_recalculation(user_id=user_id)
        return True

    def schedule_source_recalculation(self, user_id: int, source_id: int) -> bool:
        """Фоновый пересчёт непросмотренного после активации источника."""
        if self._viewed_status_provider is None:
            return False
        if not self._catalog.load_user_interests(user_id):
            return False
        self._scheduler.schedule_source_recalculation(user_id=user_id, source_id=source_id)
        return True

    def _recalculate(self, user_id: int, *, source_id: int | None) -> list[ContentUnitRef]:
        if self._viewed_status_provider is None or self._selector is None:
            return []
        if not self._catalog.load_user_interests(user_id):
            return []
        candidates = self._catalog.candidate_units(user_id, source_id)
        selected = self._selector.select(user_id, candidates)
        for ref in selected:
            self._scheduler.schedule_content_unit(
                user_id=user_id, kind=ref.kind, unit_id=ref.unit_id
            )
        return selected

    async def _score_locked(
        self, user_id: int, ref: ContentUnitRef
    ) -> UserRelevanceOutcome:
        try:
            use_case = ContentUnitAnalysisUseCase(
                session_factory=self._session_factory,
                kind=ref.kind,
                repository=self._repository,
                relevance_service=self._relevance_service_factory(),
                # Внутренний повтор здесь только тратил бы AI-вызов на заведомо
                # старый список: актуальные интересы перечитывает внешний цикл.
                max_attempts=1,
            )
        except LLMError as error:
            return self._outcome(
                user_id,
                ref.kind,
                ref.unit_id,
                UserRelevanceStatus.FAILED,
                error=str(error),
            )
        attempts = 0
        while attempts < self._max_attempts:
            attempts += 1
            interests = self._catalog.load_user_interests(user_id)
            if not interests:
                return self._outcome(
                    user_id,
                    ref.kind,
                    ref.unit_id,
                    UserRelevanceStatus.SKIPPED,
                    attempts,
                    error="У пользователя нет интересов.",
                )
            if not self._catalog.has_enabled_source_access(user_id, ref):
                return self._outcome(
                    user_id,
                    ref.kind,
                    ref.unit_id,
                    UserRelevanceStatus.SKIPPED,
                    attempts,
                    error="Нет включённого источника этого контента.",
                )
            try:
                analysis = await use_case.run(
                    ref.unit_id,
                    operation=AnalysisOperation.RELEVANCE,
                    user_id=user_id,
                    interests=interests,
                )
            except AnalysisRequestError as error:
                return self._outcome(
                    user_id,
                    ref.kind,
                    ref.unit_id,
                    UserRelevanceStatus.SKIPPED,
                    attempts,
                    error=str(error),
                )
            except (AnalysisStorageError, LLMError) as error:
                return self._outcome(
                    user_id,
                    ref.kind,
                    ref.unit_id,
                    UserRelevanceStatus.FAILED,
                    attempts,
                    error=str(error),
                )
            step = analysis.relevance
            if step is None:
                return self._outcome(
                    user_id,
                    ref.kind,
                    ref.unit_id,
                    UserRelevanceStatus.FAILED,
                    attempts,
                    error="Нет результата оценки релевантности.",
                )
            if step.status is AnalysisStatus.CURRENT:
                return self._outcome(
                    user_id,
                    ref.kind,
                    ref.unit_id,
                    UserRelevanceStatus.CURRENT,
                    attempts,
                    reused=step.reused,
                )
            if step.status is AnalysisStatus.STALE_CANDIDATE:
                # Интересы изменились во время AI-вызова: ответ не сохранён как
                # current. Перечитываем список и повторяем с актуальными данными.
                continue
            if step.status is AnalysisStatus.FAILED:
                return self._outcome(
                    user_id,
                    ref.kind,
                    ref.unit_id,
                    UserRelevanceStatus.FAILED,
                    attempts,
                    error=step.error or "Не удалось получить оценку релевантности.",
                )
            return self._outcome(
                user_id,
                ref.kind,
                ref.unit_id,
                UserRelevanceStatus.SKIPPED,
                attempts,
                error=step.error,
            )
        return self._outcome(
            user_id,
            ref.kind,
            ref.unit_id,
            UserRelevanceStatus.FAILED,
            attempts,
            error="Интересы изменились во время оценки; актуальная оценка не сохранена.",
        )

    @staticmethod
    def _outcome(
        user_id: int,
        kind: str,
        unit_id: int,
        status: UserRelevanceStatus,
        attempts: int = 1,
        *,
        reused: bool | None = None,
        error: str | None = None,
    ) -> UserRelevanceOutcome:
        return UserRelevanceOutcome(
            user_id=user_id,
            kind=kind,
            unit_id=unit_id,
            status=status,
            attempts=attempts,
            reused=reused,
            error=error,
        )


def build_default_service(
    *,
    viewed_status_provider: ViewedStatusProvider | None = None,
    scheduler: RelevanceTaskScheduler | None = None,
) -> UserRelevanceService:
    """Собрать сервис на реальных зависимостях приложения.

    Провайдер статуса просмотра берётся из единой точки подключения
    `get_viewed_status_provider()`, если явный аргумент не передан. По умолчанию
    функции подключения возвращается `None`: массового ретроспективного
    пересчёта нет. AI-адаптер создаётся лениво, поэтому хуки изменения интересов
    не требуют ключей провайдера.
    """
    from app.workers.scheduler import CeleryTaskScheduler

    provider = (
        viewed_status_provider
        if viewed_status_provider is not None
        else viewed_status.get_viewed_status_provider()
    )

    def session_factory() -> Session:
        return Session(get_engine())

    repository = InterestGuardedRepository()
    catalog = UserRelevanceCatalog(session_factory=session_factory)
    selector: UnviewedCandidateSelector | None = None
    if provider is not None:

        def has_current_summary(ref: ContentUnitRef) -> bool:
            try:
                with session_factory() as session:
                    snapshot = repository.load_snapshot(
                        session, kind=ref.kind, unit_id=ref.unit_id
                    )
            except (FeedItemInClusterError, ValueError):
                return False
            return snapshot is not None and snapshot.summary_result is not None

        selector = UnviewedCandidateSelector(
            viewed_status_provider=provider,
            has_current_summary=has_current_summary,
        )

    return UserRelevanceService(
        session_factory=session_factory,
        relevance_service_factory=lambda: RelevanceService(
            create_relevance_adapter(get_settings())
        ),
        repository=repository,
        catalog=catalog,
        scheduler=scheduler if scheduler is not None else CeleryTaskScheduler(),
        viewed_status_provider=provider,
        selector=selector,
        lock=PostgresAdvisoryLock(),
    )


def notify_interests_changed(
    user_id: int, *, service: UserRelevanceService | None = None
) -> bool:
    """Узкий хук после фактического изменения списка интересов.

    Без `ViewedStatusProvider` пересчёт истории не планируется. Ошибка
    планировщика не отменяет уже сохранённое изменение интересов.
    """
    return _notify(
        service,
        "интересов",
        lambda resolved: resolved.schedule_interests_recalculation(user_id),
    )


def notify_source_activated(
    user_id: int, source_id: int, *, service: UserRelevanceService | None = None
) -> bool:
    """Узкий хук после подключения или повторного включения источника."""
    return _notify(
        service,
        "активации источника",
        lambda resolved: resolved.schedule_source_recalculation(user_id, source_id),
    )


def _notify(
    service: UserRelevanceService | None,
    reason: str,
    action: Callable[[UserRelevanceService], bool],
) -> bool:
    try:
        resolved = service if service is not None else build_default_service()
        return action(resolved)
    except Exception:
        logger.exception(
            "Не удалось запланировать пересчёт релевантности после изменения %s.", reason
        )
        return False
