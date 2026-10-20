from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session, load_only

from app.ai.analysis_metadata import (
    FeedItemAIMetadata,
    ImportanceResultHistory,
    RelevanceResultHistory,
    StaleImportanceCandidate,
    StaleRelevanceCandidate,
    StaleSummaryCandidate,
    StoredImportanceResult,
    StoredRelevanceResult,
    StoredSummaryResult,
    SummaryResultHistory,
    UserItemScoreAIMetadata,
    content_input_fingerprint,
    summary_lineage_fingerprint,
)
from app.ai.dto import (
    ContentUnitInput,
    ContentUnitPublication,
    ImportanceResult,
    RelevanceResult,
    SummaryResult,
)
from app.ai.importance import IMPORTANCE_AGGREGATION_VERSION, IMPORTANCE_WEIGHTS
from app.models.feed_item import FeedItem
from app.models.source import Source
from app.models.user_item_score import UserItemScore


class SaveDisposition(StrEnum):
    CURRENT = "current"
    STALE_CANDIDATE = "stale_candidate"
    NOT_FOUND = "not_found"


class UnsupportedContentUnitKind(ValueError):
    pass


class InvalidAnalysisMetadata(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class FeedItemAnalysisSnapshot:
    unit: ContentUnitInput
    input_fingerprint: str
    summary_result: SummaryResult | None
    importance_result: ImportanceResult | None


@dataclass(frozen=True, slots=True)
class _LoadedFeedItem:
    row: FeedItem
    unit: ContentUnitInput
    input_fingerprint: str
    metadata: FeedItemAIMetadata


def content_unit_from_feed_item(feed_item: FeedItem, source_name: str) -> ContentUnitInput:
    try:
        return ContentUnitInput(
            kind="feed_item",
            id=feed_item.id,
            feed_items=[
                ContentUnitPublication(
                    feed_item_id=feed_item.id,
                    body_text=feed_item.body_text,
                    original_url=feed_item.original_url,
                    title=feed_item.title,
                    published_at=feed_item.published_at,
                    source_name=source_name,
                    extracted_entities=feed_item.extracted_entities,
                )
            ],
        )
    except ValidationError:
        raise ValueError("FeedItem не соответствует контракту единицы контента.") from None


class FeedItemAnalysisRepository:
    """Работает только с FeedItem и не фиксирует транзакции вызывающей сессии.

    Чтение снимка и каждая запись должны выполняться в короткой отдельной
    транзакции; сетевые запросы выполняются вызывающей стороной между ними.
    Записывающие методы блокируют FeedItem и связанный Source перед повторным
    чтением входа и условным сохранением. Членство в кластере не проверяется:
    кластерная ORM в этом срезе не интегрирована.
    """

    def load_snapshot(
        self,
        session: Session,
        *,
        kind: str,
        unit_id: int,
        lock: bool = False,
    ) -> FeedItemAnalysisSnapshot | None:
        """Загружает FeedItem; до интеграции кластеров членство не проверяется."""
        if kind != "feed_item":
            if kind == "cluster":
                raise UnsupportedContentUnitKind("Загрузка кластеров пока не поддерживается.")
            raise UnsupportedContentUnitKind("Неизвестный вид единицы контента.")

        loaded = self._load_feed_item(session, unit_id, lock=lock)
        return None if loaded is None else self._snapshot_from_loaded(loaded)

    def get_relevance_prior(
        self,
        session: Session,
        *,
        user_id: int,
        feed_item_id: int,
        expected_summary_lineage_fingerprint: str,
        interests_fingerprint: str,
    ) -> RelevanceResult | None:
        snapshot = self.load_snapshot(session, kind="feed_item", unit_id=feed_item_id)
        if snapshot is None or snapshot.summary_result is None:
            return None
        if (
            summary_lineage_fingerprint(snapshot.summary_result)
            != expected_summary_lineage_fingerprint
        ):
            return None

        row = session.scalar(
            select(UserItemScore).where(
                UserItemScore.user_id == user_id,
                UserItemScore.feed_item_id == feed_item_id,
            )
        )
        if row is None or row.relevance_score is None:
            return None
        last_good = self._read_score_metadata(row).relevance.last_good
        if (
            last_good is None
            or last_good.status != "current"
            or last_good.input_fingerprint != last_good.result.fingerprint
            or last_good.source_input_fingerprint != snapshot.input_fingerprint
            or last_good.summary_lineage_fingerprint != expected_summary_lineage_fingerprint
            or last_good.interests_fingerprint != interests_fingerprint
            or row.relevance_score != last_good.result.relevance
        ):
            return None
        return last_good.result

    def save_summary(
        self,
        session: Session,
        *,
        feed_item_id: int,
        expected_input_fingerprint: str,
        result: SummaryResult,
    ) -> SaveDisposition:
        validated_result = SummaryResult.model_validate(result)
        loaded = self._load_feed_item(session, feed_item_id, lock=True)
        if loaded is None:
            return SaveDisposition.NOT_FOUND

        metadata = loaded.metadata
        if loaded.input_fingerprint != expected_input_fingerprint:
            metadata, _ = self._mark_inconsistent_outputs(session, loaded)
            summary = metadata.summary.model_copy(
                update={
                    "stale_candidate": StaleSummaryCandidate(
                        input_fingerprint=expected_input_fingerprint,
                        result=validated_result,
                    )
                }
            )
            self._write_feed_metadata(loaded.row, metadata.model_copy(update={"summary": summary}))
            session.flush()
            return SaveDisposition.STALE_CANDIDATE

        previous = metadata.summary.last_good
        changed = (
            previous is None
            or previous.status != "current"
            or previous.input_fingerprint != loaded.input_fingerprint
            or previous.result.fingerprint != validated_result.fingerprint
            or previous.result.summary != validated_result.summary
            or loaded.row.summary != validated_result.summary
        )
        if changed:
            metadata = self._invalidate_importance(loaded.row, metadata)
            self._invalidate_relevance(session, feed_item_id)

        summary = SummaryResultHistory(
            last_good=StoredSummaryResult(
                status="current",
                input_fingerprint=loaded.input_fingerprint,
                result=validated_result,
            ),
            stale_candidate=metadata.summary.stale_candidate,
        )
        loaded.row.summary = validated_result.summary
        self._write_feed_metadata(loaded.row, metadata.model_copy(update={"summary": summary}))
        session.flush()
        return SaveDisposition.CURRENT

    def save_importance(
        self,
        session: Session,
        *,
        feed_item_id: int,
        expected_input_fingerprint: str,
        expected_summary_lineage_fingerprint: str,
        result: ImportanceResult,
    ) -> SaveDisposition:
        validated_result = ImportanceResult.model_validate(result)
        loaded = self._load_feed_item(session, feed_item_id, lock=True)
        if loaded is None:
            return SaveDisposition.NOT_FOUND

        current_summary = self._current_summary(loaded)
        is_current = (
            loaded.input_fingerprint == expected_input_fingerprint
            and current_summary is not None
            and summary_lineage_fingerprint(current_summary) == expected_summary_lineage_fingerprint
        )
        if not is_current:
            metadata, _ = self._mark_inconsistent_outputs(session, loaded)
            importance = metadata.importance.model_copy(
                update={
                    "stale_candidate": StaleImportanceCandidate(
                        input_fingerprint=expected_input_fingerprint,
                        summary_lineage_fingerprint=expected_summary_lineage_fingerprint,
                        result=validated_result,
                    )
                }
            )
            self._write_feed_metadata(
                loaded.row, metadata.model_copy(update={"importance": importance})
            )
            session.flush()
            return SaveDisposition.STALE_CANDIDATE

        importance = ImportanceResultHistory(
            last_good=StoredImportanceResult(
                status="current",
                input_fingerprint=expected_input_fingerprint,
                summary_lineage_fingerprint=expected_summary_lineage_fingerprint,
                result=validated_result,
            ),
            stale_candidate=loaded.metadata.importance.stale_candidate,
        )
        loaded.row.importance_score = validated_result.total_score
        metadata = loaded.metadata.model_copy(update={"importance": importance})
        self._write_feed_metadata(loaded.row, metadata)
        session.flush()
        return SaveDisposition.CURRENT

    def save_relevance(
        self,
        session: Session,
        *,
        user_id: int,
        feed_item_id: int,
        source_input_fingerprint: str,
        expected_summary_lineage_fingerprint: str,
        interests_fingerprint: str,
        result: RelevanceResult,
    ) -> SaveDisposition:
        validated_result = RelevanceResult.model_validate(result)
        loaded = self._load_feed_item(session, feed_item_id, lock=True)
        if loaded is None:
            return SaveDisposition.NOT_FOUND

        current_summary = self._current_summary(loaded)
        is_current = (
            loaded.input_fingerprint == source_input_fingerprint
            and current_summary is not None
            and summary_lineage_fingerprint(current_summary) == expected_summary_lineage_fingerprint
        )
        row = session.scalar(
            select(UserItemScore)
            .where(
                UserItemScore.user_id == user_id,
                UserItemScore.feed_item_id == feed_item_id,
            )
            .with_for_update()
        )
        if row is None:
            row = UserItemScore(user_id=user_id, feed_item_id=feed_item_id)
            session.add(row)

        metadata = self._read_score_metadata(row)
        if not is_current:
            history = self._mark_relevance_stale_if_inconsistent(
                row,
                metadata.relevance,
                current_input_fingerprint=loaded.input_fingerprint,
                current_summary_lineage_fingerprint=(
                    summary_lineage_fingerprint(current_summary)
                    if current_summary is not None
                    else None
                ),
                requested_interests_fingerprint=interests_fingerprint,
            )
            history = history.model_copy(
                update={
                    "stale_candidate": StaleRelevanceCandidate(
                        input_fingerprint=validated_result.fingerprint,
                        source_input_fingerprint=source_input_fingerprint,
                        summary_lineage_fingerprint=expected_summary_lineage_fingerprint,
                        interests_fingerprint=interests_fingerprint,
                        result=validated_result,
                    )
                }
            )
            self._write_score_metadata(row, UserItemScoreAIMetadata(relevance=history))
            session.flush()
            return SaveDisposition.STALE_CANDIDATE

        history = RelevanceResultHistory(
            last_good=StoredRelevanceResult(
                status="current",
                input_fingerprint=validated_result.fingerprint,
                source_input_fingerprint=source_input_fingerprint,
                summary_lineage_fingerprint=expected_summary_lineage_fingerprint,
                interests_fingerprint=interests_fingerprint,
                result=validated_result,
            ),
            stale_candidate=metadata.relevance.stale_candidate,
        )
        row.relevance_score = validated_result.relevance
        row.rank_score = None
        row.reason = None
        row.calculated_at = func.current_timestamp()
        self._write_score_metadata(row, UserItemScoreAIMetadata(relevance=history))
        session.flush()
        return SaveDisposition.CURRENT

    def mark_feed_item_stale(self, session: Session, *, feed_item_id: int) -> bool:
        loaded = self._load_feed_item(session, feed_item_id, lock=True)
        if loaded is None:
            return False
        metadata, changed = self._mark_inconsistent_outputs(session, loaded)
        if metadata != loaded.metadata:
            self._write_feed_metadata(loaded.row, metadata)
        if changed:
            session.flush()
        return changed

    def _load_feed_item(
        self, session: Session, feed_item_id: int, *, lock: bool
    ) -> _LoadedFeedItem | None:
        statement = (
            select(FeedItem, Source.name)
            .options(
                load_only(
                    FeedItem.id,
                    FeedItem.source_id,
                    FeedItem.title,
                    FeedItem.body_text,
                    FeedItem.original_url,
                    FeedItem.published_at,
                    FeedItem.extracted_entities,
                    FeedItem.summary,
                    FeedItem.importance_score,
                    FeedItem.ai_metadata,
                    raiseload=True,
                )
            )
            .join(Source, Source.id == FeedItem.source_id)
            .where(FeedItem.id == feed_item_id)
        )
        if lock:
            statement = statement.with_for_update(of=(FeedItem, Source))
        record = session.execute(statement).one_or_none()
        if record is None:
            return None
        row, source_name = record
        unit = content_unit_from_feed_item(row, source_name)
        return _LoadedFeedItem(
            row=row,
            unit=unit,
            input_fingerprint=content_input_fingerprint(unit),
            metadata=self._read_feed_metadata(row),
        )

    @staticmethod
    def _snapshot_from_loaded(loaded: _LoadedFeedItem) -> FeedItemAnalysisSnapshot:
        summary = FeedItemAnalysisRepository._current_summary(loaded)
        importance = FeedItemAnalysisRepository._current_importance(loaded, summary)
        return FeedItemAnalysisSnapshot(
            unit=loaded.unit,
            input_fingerprint=loaded.input_fingerprint,
            summary_result=summary,
            importance_result=importance,
        )

    @staticmethod
    def _current_summary(loaded: _LoadedFeedItem) -> SummaryResult | None:
        last_good = loaded.metadata.summary.last_good
        if (
            last_good is None
            or last_good.status != "current"
            or last_good.input_fingerprint != loaded.input_fingerprint
            or loaded.row.summary != last_good.result.summary
        ):
            return None
        return last_good.result

    @staticmethod
    def _current_importance(
        loaded: _LoadedFeedItem, summary: SummaryResult | None
    ) -> ImportanceResult | None:
        last_good = loaded.metadata.importance.last_good
        if (
            summary is None
            or last_good is None
            or last_good.status != "current"
            or last_good.input_fingerprint != loaded.input_fingerprint
            or last_good.summary_lineage_fingerprint != summary_lineage_fingerprint(summary)
            or last_good.result.aggregation_version != IMPORTANCE_AGGREGATION_VERSION
            or last_good.result.weights != IMPORTANCE_WEIGHTS
            or loaded.row.importance_score != last_good.result.total_score
        ):
            return None
        return last_good.result

    def _mark_inconsistent_outputs(
        self, session: Session, loaded: _LoadedFeedItem
    ) -> tuple[FeedItemAIMetadata, bool]:
        metadata = loaded.metadata
        summary = self._current_summary(loaded)
        importance_current = self._current_importance(loaded, summary) is not None
        changed = False

        summary_history = metadata.summary
        if summary_history.last_good is not None and summary is None:
            if summary_history.last_good.status != "stale":
                changed = True
            summary_history = summary_history.model_copy(
                update={
                    "last_good": summary_history.last_good.model_copy(update={"status": "stale"})
                }
            )
        if loaded.row.summary is not None and summary is None:
            loaded.row.summary = None
            changed = True

        importance_history = metadata.importance
        if importance_history.last_good is not None and not importance_current:
            if importance_history.last_good.status != "stale":
                changed = True
            importance_history = importance_history.model_copy(
                update={
                    "last_good": importance_history.last_good.model_copy(update={"status": "stale"})
                }
            )
        if loaded.row.importance_score is not None and not importance_current:
            loaded.row.importance_score = None
            changed = True

        if summary is None:
            changed = self._invalidate_relevance(session, loaded.row.id) or changed

        updated = metadata.model_copy(
            update={"summary": summary_history, "importance": importance_history}
        )
        return updated, changed

    @staticmethod
    def _invalidate_importance(row: FeedItem, metadata: FeedItemAIMetadata) -> FeedItemAIMetadata:
        history = metadata.importance
        if history.last_good is not None:
            history = history.model_copy(
                update={"last_good": history.last_good.model_copy(update={"status": "stale"})}
            )
        row.importance_score = None
        return metadata.model_copy(update={"importance": history})

    def _invalidate_relevance(self, session: Session, feed_item_id: int) -> bool:
        rows = session.scalars(
            select(UserItemScore)
            .where(UserItemScore.feed_item_id == feed_item_id)
            .with_for_update()
        ).all()
        changed = False
        for row in rows:
            metadata = self._read_score_metadata(row)
            history = metadata.relevance
            if history.last_good is not None:
                if history.last_good.status != "stale":
                    changed = True
                history = history.model_copy(
                    update={"last_good": history.last_good.model_copy(update={"status": "stale"})}
                )
            if (
                row.relevance_score is not None
                or row.rank_score is not None
                or row.reason is not None
            ):
                changed = True
            row.relevance_score = None
            row.rank_score = None
            row.reason = None
            updated_metadata = UserItemScoreAIMetadata(relevance=history)
            if updated_metadata != metadata:
                self._write_score_metadata(row, updated_metadata)
        return changed

    @staticmethod
    def _mark_relevance_stale_if_inconsistent(
        row: UserItemScore,
        history: RelevanceResultHistory,
        *,
        current_input_fingerprint: str,
        current_summary_lineage_fingerprint: str | None,
        requested_interests_fingerprint: str,
    ) -> RelevanceResultHistory:
        last_good = history.last_good
        if last_good is None:
            row.relevance_score = None
            row.rank_score = None
            row.reason = None
            return history

        if (
            last_good.input_fingerprint != last_good.result.fingerprint
            or last_good.source_input_fingerprint != current_input_fingerprint
            or last_good.summary_lineage_fingerprint != current_summary_lineage_fingerprint
            or last_good.interests_fingerprint != requested_interests_fingerprint
        ):
            row.relevance_score = None
            row.rank_score = None
            row.reason = None
            return history.model_copy(
                update={"last_good": last_good.model_copy(update={"status": "stale"})}
            )
        return history

    @staticmethod
    def _read_feed_metadata(row: FeedItem) -> FeedItemAIMetadata:
        if row.ai_metadata is None:
            return FeedItemAIMetadata()
        try:
            return FeedItemAIMetadata.model_validate(row.ai_metadata)
        except ValidationError:
            raise InvalidAnalysisMetadata(
                "Метаданные анализа FeedItem имеют неверный формат."
            ) from None

    @staticmethod
    def _read_score_metadata(row: UserItemScore) -> UserItemScoreAIMetadata:
        if row.analysis_metadata is None:
            return UserItemScoreAIMetadata()
        try:
            return UserItemScoreAIMetadata.model_validate(row.analysis_metadata)
        except ValidationError:
            raise InvalidAnalysisMetadata(
                "Метаданные оценки пользователя имеют неверный формат."
            ) from None

    @staticmethod
    def _write_feed_metadata(row: FeedItem, metadata: FeedItemAIMetadata) -> None:
        row.ai_metadata = metadata.model_dump(mode="json", exclude_none=True)

    @staticmethod
    def _write_score_metadata(row: UserItemScore, metadata: UserItemScoreAIMetadata) -> None:
        row.analysis_metadata = metadata.model_dump(mode="json", exclude_none=True)
