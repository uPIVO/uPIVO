from decimal import Decimal
from typing import Any

from sqlalchemy import CheckConstraint, UniqueConstraint
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import JSONB

from app.ai.analysis_metadata import content_input_fingerprint, interests_input_fingerprint
from app.ai.dto import (
    ImportanceResult,
    ImportanceWeights,
    RelevanceResult,
    SummaryResult,
)
from app.ai.repository import (
    FeedItemAnalysisRepository,
    SaveDisposition,
    UnsupportedContentUnitKind,
    content_unit_from_feed_item,
)
from app.models.feed_item import FeedItem
from app.models.user_item_score import UserItemScore


class _Rows:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return self._rows


class _OneRow:
    def __init__(self, row: Any) -> None:
        self._row = row

    def one_or_none(self) -> Any:
        return self._row


class _Session:
    def __init__(self, feed_item: FeedItem, *, source_name: str = "Источник") -> None:
        self.feed_item = feed_item
        self.source_name = source_name
        self.score: UserItemScore | None = None
        self.scores: list[UserItemScore] = []
        self.flush_count = 0
        self.execute_count = 0
        self.statements: list[Any] = []

    def execute(self, statement: Any) -> _OneRow:
        self.execute_count += 1
        self.statements.append(statement)
        return _OneRow((self.feed_item, self.source_name))

    def scalar(self, _statement: Any) -> UserItemScore | None:
        return self.score

    def scalars(self, _statement: Any) -> _Rows:
        return _Rows(self.scores)

    def add(self, row: UserItemScore) -> None:
        self.score = row
        self.scores.append(row)

    def flush(self) -> None:
        self.flush_count += 1


def _feed_item() -> FeedItem:
    return FeedItem(
        id=7,
        source_id=2,
        external_id="post-7",
        title="Заголовок",
        body_text="Нормализованное содержимое",
        original_url="https://example.test/item",
        published_at=None,
        raw_payload={"secret": "сырой текст не должен передаваться в анализ"},
        extracted_entities={"tags": ["AI"]},
        summary=None,
        importance_score=None,
        ai_metadata=None,
    )


def _summary(summary: str, fingerprint: str) -> SummaryResult:
    return SummaryResult(
        summary=summary,
        fingerprint=f"v1:{fingerprint * 64}",
        provider="openrouter",
        preset="@preset/test",
        cost_usd=Decimal("0.001"),
    )


def _importance(fingerprint: str, total: float = 3.0) -> ImportanceResult:
    return ImportanceResult(
        scale_score=2.0,
        significance_score=1.0,
        reaction_score=1.0,
        fingerprint=f"v1:{fingerprint * 64}",
        provider="typesafe",
        model="jev-test",
        total_score=total,
        weights=ImportanceWeights(scale=0.5, significance=1.0, reaction=1.0),
        aggregation_version="content-importance-weighted-v1",
    )


def _relevance(fingerprint: str, value: float = 0.75) -> RelevanceResult:
    return RelevanceResult(
        relevance=value,
        fingerprint=f"v1:{fingerprint * 64}",
        provider="typesafe",
        model="jev-test",
    )


def test_feed_item_has_nullable_jsonb_analysis_metadata() -> None:
    column = FeedItem.__table__.c.ai_metadata

    assert isinstance(column.type, JSONB)
    assert column.nullable is True


def test_user_item_score_model_matches_feed_only_storage_contract() -> None:
    table = UserItemScore.__table__
    unique_constraints = {
        tuple(column.name for column in constraint.columns)
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    checks = [
        constraint for constraint in table.constraints if isinstance(constraint, CheckConstraint)
    ]
    foreign_keys = {foreign_key.parent.name: foreign_key for foreign_key in table.foreign_keys}

    assert table.c.feed_item_id.nullable is False
    assert table.c.user_id.nullable is False
    assert table.c.relevance_score.nullable is True
    assert table.c.rank_score.nullable is True
    assert table.c.reason.nullable is True
    assert table.c.analysis_metadata.nullable is True
    assert isinstance(table.c.analysis_metadata.type, JSONB)
    assert ("user_id", "feed_item_id") in unique_constraints
    assert set(foreign_keys) == {"user_id", "feed_item_id"}
    assert all(foreign_key.ondelete == "CASCADE" for foreign_key in foreign_keys.values())
    assert any("relevance_score" in str(check.sqltext) for check in checks)
    assert "cluster_id" not in table.c


def test_feed_item_mapping_includes_source_but_excludes_raw_payload() -> None:
    row = _feed_item()
    unit = content_unit_from_feed_item(row, "Первый источник")
    fingerprint = content_input_fingerprint(unit)

    assert unit.kind == "feed_item"
    assert unit.id == row.id
    assert unit.feed_items[0].feed_item_id == row.id
    assert unit.feed_items[0].source_name == "Первый источник"
    assert unit.feed_items[0].extracted_entities == row.extracted_entities
    assert "raw_payload" not in unit.model_dump()
    assert fingerprint != content_input_fingerprint(
        content_unit_from_feed_item(row, "Другой источник")
    )


def test_locked_snapshot_omits_raw_payload_and_locks_source_with_feed_item() -> None:
    session = _Session(_feed_item())

    snapshot = FeedItemAnalysisRepository().load_snapshot(
        session, kind="feed_item", unit_id=7, lock=True
    )

    assert snapshot is not None
    statement = str(session.statements[0].compile(dialect=postgresql.dialect()))
    selected_columns = statement.split(" FROM ", maxsplit=1)[0]
    assert "raw_payload" not in selected_columns
    assert "processing_status" not in selected_columns
    assert "FOR UPDATE OF feed_items, sources" in statement


def test_repository_rejects_cluster_without_querying_cluster_tables() -> None:
    session = _Session(_feed_item())

    try:
        FeedItemAnalysisRepository().load_snapshot(session, kind="cluster", unit_id=7)
    except UnsupportedContentUnitKind as exc:
        assert "пока не поддерживается" in str(exc)
    else:
        raise AssertionError("Для кластера ожидалась явная ошибка")

    assert session.execute_count == 0


def test_summary_save_keeps_one_stale_candidate_and_never_publishes_it() -> None:
    row = _feed_item()
    session = _Session(row)
    repository = FeedItemAnalysisRepository()
    initial = repository.load_snapshot(session, kind="feed_item", unit_id=row.id)
    old_result = _summary("Старая выжимка", "a")

    assert initial is not None
    assert (
        repository.save_summary(
            session,
            feed_item_id=row.id,
            expected_input_fingerprint=initial.input_fingerprint,
            result=old_result,
        )
        is SaveDisposition.CURRENT
    )
    assert row.summary == old_result.summary

    row.body_text = "Состав исходных данных изменился"
    stale_result = _summary("Оплаченный ответ для старого снимка", "b")
    assert (
        repository.save_summary(
            session,
            feed_item_id=row.id,
            expected_input_fingerprint=initial.input_fingerprint,
            result=stale_result,
        )
        is SaveDisposition.STALE_CANDIDATE
    )

    saved = FeedItemAnalysisRepository._read_feed_metadata(row)
    current = repository.load_snapshot(session, kind="feed_item", unit_id=row.id)
    assert row.summary is None
    assert saved.summary.last_good.status == "stale"
    assert saved.summary.last_good.result == old_result
    assert saved.summary.stale_candidate.result == stale_result
    assert current is not None
    assert current.summary_result is None

    replacement = _summary("Ответ для следующей попытки", "c")
    assert (
        repository.save_summary(
            session,
            feed_item_id=row.id,
            expected_input_fingerprint=initial.input_fingerprint,
            result=replacement,
        )
        is SaveDisposition.STALE_CANDIDATE
    )
    saved = FeedItemAnalysisRepository._read_feed_metadata(row)
    assert saved.summary.last_good.result == old_result
    assert saved.summary.stale_candidate.result == replacement


def test_new_summary_invalidates_importance_and_user_score_projections() -> None:
    row = _feed_item()
    session = _Session(row)
    repository = FeedItemAnalysisRepository()
    snapshot = repository.load_snapshot(session, kind="feed_item", unit_id=row.id)
    assert snapshot is not None
    summary = _summary("Первая выжимка", "a")
    assert (
        repository.save_summary(
            session,
            feed_item_id=row.id,
            expected_input_fingerprint=snapshot.input_fingerprint,
            result=summary,
        )
        is SaveDisposition.CURRENT
    )
    assert (
        repository.save_importance(
            session,
            feed_item_id=row.id,
            expected_input_fingerprint=snapshot.input_fingerprint,
            summary_fingerprint=summary.fingerprint,
            result=_importance("b"),
        )
        is SaveDisposition.CURRENT
    )
    assert (
        repository.save_relevance(
            session,
            user_id=9,
            feed_item_id=row.id,
            source_input_fingerprint=snapshot.input_fingerprint,
            summary_fingerprint=summary.fingerprint,
            interests_fingerprint=interests_input_fingerprint(["тема"]),
            result=_relevance("c"),
        )
        is SaveDisposition.CURRENT
    )
    assert session.score is not None
    assert session.score.relevance_score == 0.75
    assert row.importance_score == 3.0

    changed_summary = _summary("Обновлённая выжимка", "d")
    assert (
        repository.save_summary(
            session,
            feed_item_id=row.id,
            expected_input_fingerprint=snapshot.input_fingerprint,
            result=changed_summary,
        )
        is SaveDisposition.CURRENT
    )

    score_metadata = FeedItemAnalysisRepository._read_score_metadata(session.score)
    feed_metadata = FeedItemAnalysisRepository._read_feed_metadata(row)
    assert row.summary == changed_summary.summary
    assert row.importance_score is None
    assert feed_metadata.importance.last_good.status == "stale"
    assert session.score.relevance_score is None
    assert session.score.rank_score is None
    assert session.score.reason is None
    assert score_metadata.relevance.last_good.status == "stale"


def test_relevance_prior_requires_current_summary_and_matching_interest_fingerprint() -> None:
    row = _feed_item()
    session = _Session(row)
    repository = FeedItemAnalysisRepository()
    snapshot = repository.load_snapshot(session, kind="feed_item", unit_id=row.id)
    assert snapshot is not None
    summary = _summary("Текущая выжимка", "a")
    assert (
        repository.save_summary(
            session,
            feed_item_id=row.id,
            expected_input_fingerprint=snapshot.input_fingerprint,
            result=summary,
        )
        is SaveDisposition.CURRENT
    )
    interests_fingerprint = interests_input_fingerprint(["тема"])
    relevance = _relevance("c")
    assert (
        repository.save_relevance(
            session,
            user_id=9,
            feed_item_id=row.id,
            source_input_fingerprint=snapshot.input_fingerprint,
            summary_fingerprint=summary.fingerprint,
            interests_fingerprint=interests_fingerprint,
            result=relevance,
        )
        is SaveDisposition.CURRENT
    )

    assert (
        repository.get_relevance_prior(
            session,
            user_id=9,
            feed_item_id=row.id,
            summary_fingerprint=summary.fingerprint,
            interests_fingerprint=interests_fingerprint,
        )
        == relevance
    )
    assert (
        repository.get_relevance_prior(
            session,
            user_id=9,
            feed_item_id=row.id,
            summary_fingerprint=summary.fingerprint,
            interests_fingerprint=interests_input_fingerprint(["другая тема"]),
        )
        is None
    )


def test_repository_does_not_create_empty_analysis_metadata_for_unprocessed_item() -> None:
    row = _feed_item()
    session = _Session(row)

    assert FeedItemAnalysisRepository().mark_feed_item_stale(session, feed_item_id=row.id) is False
    assert row.ai_metadata is None
    assert session.flush_count == 0
