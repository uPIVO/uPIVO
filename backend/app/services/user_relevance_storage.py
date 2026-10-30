"""Хранилище персональной оценки по интересам из БД.

Guard-репозиторий проверяет в транзакции сохранения актуальный отпечаток
интересов пользователя и право на контент через включённые источники.
Каталог читает интересы, разрешает единицу контента и находит кандидатов
на экономный пересчёт.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai.analysis_metadata import interests_input_fingerprint
from app.ai.dto import RelevanceResult
from app.ai.repository import ContentUnitAnalysisRepository, SaveDisposition
from app.models.content_cluster import ClusterItem, ContentCluster
from app.models.feed_item import FeedItem
from app.models.user import User
from app.models.user_interest import UserInterest
from app.models.user_source import UserSource
from app.services.user_interests import read_interest_texts

SessionFactory = Callable[[], Session]
UnitKind = Literal["feed_item", "cluster"]
AccessChecker = Callable[..., bool]


@dataclass(frozen=True, slots=True)
class ContentUnitRef:
    """Ссылка на оцениваемую единицу: публикацию или кластер целиком."""

    kind: UnitKind
    unit_id: int


def resolve_content_unit(
    session: Session, *, kind: str, unit_id: int
) -> ContentUnitRef | None:
    """Разрешает единицу контента; публикация в кластере оценивается как кластер."""
    if kind not in ("feed_item", "cluster"):
        raise ValueError("Неизвестный вид единицы контента.")
    if kind == "cluster":
        exists = session.scalar(select(ContentCluster.id).where(ContentCluster.id == unit_id))
        return ContentUnitRef(kind="cluster", unit_id=unit_id) if exists is not None else None
    exists = session.scalar(select(FeedItem.id).where(FeedItem.id == unit_id))
    if exists is None:
        return None
    cluster_id = session.scalar(
        select(ClusterItem.cluster_id).where(ClusterItem.feed_item_id == unit_id)
    )
    if cluster_id is not None:
        return ContentUnitRef(kind="cluster", unit_id=cluster_id)
    return ContentUnitRef(kind="feed_item", unit_id=unit_id)


def unit_source_ids(session: Session, ref: ContentUnitRef) -> list[int]:
    """Источники единицы контента: один для публикации, все участники для кластера."""
    if ref.kind == "feed_item":
        source_id = session.scalar(select(FeedItem.source_id).where(FeedItem.id == ref.unit_id))
        return [] if source_id is None else [source_id]
    return list(
        session.scalars(
            select(FeedItem.source_id)
            .join(ClusterItem, ClusterItem.feed_item_id == FeedItem.id)
            .where(ClusterItem.cluster_id == ref.unit_id)
            .distinct()
            .order_by(FeedItem.source_id)
        )
    )


def has_enabled_source_access(
    session: Session, *, user_id: int, source_ids: list[int]
) -> bool:
    """Есть ли у пользователя хотя бы один включённый источник из набора."""
    if not source_ids:
        return False
    return (
        session.scalar(
            select(UserSource.id)
            .where(
                UserSource.user_id == user_id,
                UserSource.enabled.is_(True),
                UserSource.source_id.in_(source_ids),
            )
            .limit(1)
        )
        is not None
    )


def eligible_user_ids(session: Session, source_ids: list[int]) -> list[int]:
    """Пользователи с включённым источником единицы и непустым списком интересов."""
    if not source_ids:
        return []
    return list(
        session.scalars(
            select(UserSource.user_id)
            .join(UserInterest, UserInterest.user_id == UserSource.user_id)
            .where(
                UserSource.enabled.is_(True),
                UserSource.source_id.in_(source_ids),
            )
            .distinct()
            .order_by(UserSource.user_id)
        )
    )


class InterestGuardedRepository(ContentUnitAnalysisRepository):
    """Сохраняет персональную оценку только при актуальных интересах и доступе.

    Перед записью в той же транзакции блокируется строка `users`: так изменения
    списка интересов и сохранение результата не проходят одновременно.
    Ответ, посчитанный для устаревшего списка, не становится `current`:
    вызывающая сторона получает `stale_candidate` и перечитывает интересы.
    """

    def __init__(
        self,
        *,
        interests_reader: Callable[[Session, int], list[str]] | None = None,
        access_checker: AccessChecker | None = None,
    ) -> None:
        self._interests_reader = interests_reader or self._read_interests
        self._access_checker = access_checker or self._check_access

    def save_relevance(
        self,
        session: Session,
        *,
        user_id: int,
        kind: str = "feed_item",
        unit_id: int | None = None,
        feed_item_id: int | None = None,
        source_input_fingerprint: str,
        expected_summary_lineage_fingerprint: str,
        interests_fingerprint: str,
        result: RelevanceResult,
    ) -> SaveDisposition:
        target_id = self._target_id(kind, unit_id, feed_item_id)
        session.execute(select(User.id).where(User.id == user_id).with_for_update())
        if not self._access_checker(
            session, user_id=user_id, kind=kind, unit_id=target_id
        ):
            return SaveDisposition.NOT_FOUND
        if (
            interests_input_fingerprint(self._interests_reader(session, user_id))
            != interests_fingerprint
        ):
            return SaveDisposition.STALE_CANDIDATE
        return super().save_relevance(
            session,
            user_id=user_id,
            kind=kind,
            unit_id=target_id,
            source_input_fingerprint=source_input_fingerprint,
            expected_summary_lineage_fingerprint=expected_summary_lineage_fingerprint,
            interests_fingerprint=interests_fingerprint,
            result=result,
        )

    def _check_access(self, session: Session, *, user_id: int, kind: str, unit_id: int) -> bool:
        ref = ContentUnitRef(kind=kind, unit_id=unit_id)
        return has_enabled_source_access(
            session, user_id=user_id, source_ids=unit_source_ids(session, ref)
        )

    @staticmethod
    def _read_interests(session: Session, user_id: int) -> list[str]:
        return read_interest_texts(session, user_id)


class UserRelevanceCatalog:
    """Чтение данных персональной оценки: интересы, единицы и кандидаты."""

    def __init__(self, *, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    def load_user_interests(self, user_id: int) -> list[str]:
        with self._session_factory() as session:
            return read_interest_texts(session, user_id)

    def resolve_unit(self, kind: str, unit_id: int) -> ContentUnitRef | None:
        with self._session_factory() as session:
            return resolve_content_unit(session, kind=kind, unit_id=unit_id)

    def has_enabled_source_access(self, user_id: int, ref: ContentUnitRef) -> bool:
        with self._session_factory() as session:
            return has_enabled_source_access(
                session, user_id=user_id, source_ids=unit_source_ids(session, ref)
            )

    def unit_source_ids(self, ref: ContentUnitRef) -> list[int]:
        with self._session_factory() as session:
            return unit_source_ids(session, ref)

    def eligible_user_ids(self, source_ids: list[int]) -> list[int]:
        with self._session_factory() as session:
            return eligible_user_ids(session, source_ids)

    def candidate_units(
        self, user_id: int, source_id: int | None = None
    ) -> list[ContentUnitRef]:
        """Единицы из сейчас включённых источников с непустой выжимкой.

        Участники кластера не возвращаются: кластер оценивается один раз.
        Готовность именно актуальной выжимки проверяет селектор.
        """
        with self._session_factory() as session:
            enabled_sources = (
                select(UserSource.source_id)
                .where(UserSource.user_id == user_id, UserSource.enabled.is_(True))
                .scalar_subquery()
            )
            member_cluster = select(ClusterItem.cluster_id).where(
                ClusterItem.feed_item_id == FeedItem.id
            )
            items = select(FeedItem.id).where(
                FeedItem.source_id.in_(enabled_sources),
                FeedItem.summary.is_not(None),
                ~member_cluster.exists(),
            )
            if source_id is not None:
                items = items.where(FeedItem.source_id == source_id)
            clusters = (
                select(ClusterItem.cluster_id)
                .join(FeedItem, FeedItem.id == ClusterItem.feed_item_id)
                .join(ContentCluster, ContentCluster.id == ClusterItem.cluster_id)
                .where(
                    FeedItem.source_id.in_(enabled_sources),
                    ContentCluster.summary.is_not(None),
                )
                .distinct()
            )
            if source_id is not None:
                clusters = clusters.where(FeedItem.source_id == source_id)
            candidates = [
                ContentUnitRef(kind="feed_item", unit_id=unit_id)
                for unit_id in session.scalars(items.order_by(FeedItem.id))
            ]
            candidates.extend(
                ContentUnitRef(kind="cluster", unit_id=cluster_id)
                for cluster_id in session.scalars(clusters.order_by(ClusterItem.cluster_id))
            )
            return candidates
