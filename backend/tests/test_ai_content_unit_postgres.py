"""Адресные проверки на отдельной PostgreSQL после alembic upgrade head.

Запускать только с ITD17_TEST_DATABASE_URL на одноразовую БД itd17_cluster_test*.
Обычный pytest пропускает эти проверки; API моделей не вызываются.
"""

import asyncio
import hashlib
import json
import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, delete, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.ai.analysis_metadata import interests_input_fingerprint, summary_lineage_fingerprint
from app.ai.dto import (
    ImportanceComponents,
    ImportanceResult,
    ImportanceWeights,
    RelevanceResult,
    SummaryResult,
)
from app.ai.importance import ImportanceService
from app.ai.repository import ContentUnitAnalysisRepository, FeedItemInClusterError, SaveDisposition
from app.ai.service import RelevanceService, SummarizationService
from app.clustering.cluster import ClusterPlan
from app.clustering.persist import _sync_members
from app.models import (
    ClusterItem,
    ContentCluster,
    FeedItem,
    Source,
    SourceType,
    User,
    UserItemScore,
)
from app.services.analyze_feed_item import ContentUnitAnalysisUseCase

_URL = os.environ.get("ITD17_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not _URL, reason="Нужна отдельная тестовая PostgreSQL")


@pytest.fixture
def engine():
    assert make_url(_URL).database.startswith("itd17_cluster_test"), (
        "Нельзя использовать рабочую БД"
    )
    db = create_engine(_URL)
    yield db
    db.dispose()


def _seed(session):
    token = uuid4().hex
    user = User(email=f"{token}@example.test", password_hash="test-only")
    source = Source(
        type=SourceType.TELEGRAM,
        name="Источник",
        external_id=token,
        canonical_url=f"https://example.test/{token}",
    )
    session.add_all([user, source])
    session.flush()
    items = [
        FeedItem(
            source_id=source.id,
            external_id=f"{token}-{i}",
            title=f"Новость {i}",
            body_text=f"Самостоятельные факты {i}",
            original_url=f"https://example.test/{token}/{i}",
        )
        for i in range(3)
    ]
    session.add_all(items)
    session.flush()
    return user, items


def _summary(label="Общее содержание"):
    return SummaryResult(
        summary=label, fingerprint=f"v1:{'a' * 64}", provider="fake", preset="test"
    )


def _importance():
    return ImportanceResult(
        scale_score=2,
        significance_score=2,
        reaction_score=1,
        total_score=4,
        weights=ImportanceWeights(scale=0.5, significance=1, reaction=1),
        aggregation_version="content-importance-weighted-v1",
        fingerprint=f"v1:{'b' * 64}",
        provider="fake",
        model="test",
    )


def _relevance():
    return RelevanceResult(
        relevance=0.8, fingerprint=f"v1:{'c' * 64}", provider="fake", model="test"
    )


def test_cluster_lifecycle_preserves_single_result_and_rejects_old_membership(engine):
    repo = ContentUnitAnalysisRepository()
    with Session(engine) as session, session.begin():
        user, items = _seed(session)
        first_id = items[0].id
        single = repo.load_snapshot(session, kind="feed_item", unit_id=first_id)
        repo.save_summary(
            session,
            feed_item_id=first_id,
            expected_input_fingerprint=single.input_fingerprint,
            result=_summary("Одиночная"),
        )
        cluster = ContentCluster(title="Событие")
        session.add(cluster)
        session.flush()
        cluster_id = cluster.id
        _sync_members(
            session,
            cluster_id,
            ClusterPlan(
                member_ids=tuple(item.id for item in items[:2]),
                primary_id=first_id,
                exact_ids=(),
                title="Событие",
            ),
        )
        with pytest.raises(FeedItemInClusterError):
            repo.load_snapshot(session, kind="feed_item", unit_id=first_id)
        assert session.get(FeedItem, first_id).summary is None
        snapshot = repo.load_snapshot(session, kind="cluster", unit_id=cluster_id)
        result = _summary()
        assert (
            repo.save_summary(
                session,
                kind="cluster",
                unit_id=cluster_id,
                expected_input_fingerprint=snapshot.input_fingerprint,
                result=result,
            )
            is SaveDisposition.CURRENT
        )
        assert (
            repo.save_importance(
                session,
                kind="cluster",
                unit_id=cluster_id,
                expected_input_fingerprint=snapshot.input_fingerprint,
                expected_summary_lineage_fingerprint=summary_lineage_fingerprint(result),
                result=_importance(),
            )
            is SaveDisposition.CURRENT
        )
        assert (
            repo.save_relevance(
                session,
                kind="cluster",
                unit_id=cluster_id,
                user_id=user.id,
                source_input_fingerprint=snapshot.input_fingerprint,
                expected_summary_lineage_fingerprint=summary_lineage_fingerprint(result),
                interests_fingerprint=interests_input_fingerprint(["тема"]),
                result=_relevance(),
            )
            is SaveDisposition.CURRENT
        )
        assert session.get(ContentCluster, cluster_id).summary == result.summary
        assert all(item.summary is None for item in items)
        score = session.scalar(select(UserItemScore).where(UserItemScore.cluster_id == cluster_id))
        assert score.feed_item_id is None and score.relevance_score == 0.8

        _sync_members(
            session,
            cluster_id,
            ClusterPlan(
                member_ids=tuple(item.id for item in items),
                primary_id=first_id,
                exact_ids=(),
                title="Событие",
            ),
        )
        session.flush()
        assert cluster.summary is None and cluster.importance_score is None
        assert score.relevance_score is None
        assert (
            repo.save_summary(
                session,
                kind="cluster",
                unit_id=cluster_id,
                expected_input_fingerprint=snapshot.input_fingerprint,
                result=result,
            )
            is SaveDisposition.STALE_CANDIDATE
        )
        assert cluster.summary is None
        fresh = repo.load_snapshot(session, kind="cluster", unit_id=cluster_id)
        assert len(fresh.unit.feed_items) == 3 and fresh.summary_result is None
        assert (
            repo.save_summary(
                session,
                kind="cluster",
                unit_id=cluster_id,
                expected_input_fingerprint=fresh.input_fingerprint,
                result=result,
            )
            is SaveDisposition.CURRENT
        )
        assert (
            repo.save_relevance(
                session,
                kind="cluster",
                unit_id=cluster_id,
                user_id=user.id,
                source_input_fingerprint=fresh.input_fingerprint,
                expected_summary_lineage_fingerprint=summary_lineage_fingerprint(result),
                interests_fingerprint=interests_input_fingerprint(["тема"]),
                result=_relevance(),
            )
            is SaveDisposition.CURRENT
        )
        session.execute(delete(ClusterItem).where(ClusterItem.cluster_id == cluster_id))
        session.delete(cluster)
        session.flush()
        assert (
            session.scalar(select(UserItemScore.id).where(UserItemScore.cluster_id == cluster_id))
            is None
        )
        assert (
            repo.load_snapshot(session, kind="feed_item", unit_id=first_id).summary_result is None
        )


def test_target_constraints_and_same_numeric_id_are_independent(engine):
    with Session(engine) as session, session.begin():
        user, items = _seed(session)
        # Создаём кластер с ID отдельного FeedItem через явный OVERRIDING SYSTEM VALUE.
        cluster_id = items[2].id
        session.execute(
            text(
                "INSERT INTO content_clusters (id, title) "
                "OVERRIDING SYSTEM VALUE VALUES (:id, 'test')"
            ),
            {"id": cluster_id},
        )
        feed_score = UserItemScore(user_id=user.id, feed_item_id=cluster_id, relevance_score=0.2)
        cluster_score = UserItemScore(user_id=user.id, cluster_id=cluster_id, relevance_score=0.9)
        session.add_all([feed_score, cluster_score])
        session.flush()
        for values in [
            dict(),
            dict(feed_item_id=cluster_id, cluster_id=cluster_id),
            dict(cluster_id=cluster_id),
            dict(feed_item_id=items[0].id, relevance_score=1.1),
        ]:
            with pytest.raises(IntegrityError), session.begin_nested():
                session.add(UserItemScore(user_id=user.id, **values))
                session.flush()
        assert (
            session.scalar(
                select(UserItemScore).where(UserItemScore.feed_item_id == cluster_id)
            ).relevance_score
            == 0.2
        )
        assert (
            session.scalar(
                select(UserItemScore).where(UserItemScore.cluster_id == cluster_id)
            ).relevance_score
            == 0.9
        )


def test_cluster_usecase_reuses_results_and_retries_changed_membership(engine):
    sessions = []
    with Session(engine) as session, session.begin():
        user, items = _seed(session)
        user_id = user.id
        ids = [item.id for item in items]
        cluster = ContentCluster(title="Одно событие")
        session.add(cluster)
        session.flush()
        cluster_id = cluster.id
        _sync_members(
            session,
            cluster_id,
            ClusterPlan(
                member_ids=tuple(ids[:2]),
                primary_id=ids[0],
                exact_ids=(),
                title=cluster.title,
            ),
        )

    def factory():
        session = Session(engine)
        sessions.append(session)
        return session

    def fingerprint(value):
        return (
            "v1:"
            + hashlib.sha256(
                json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
            ).hexdigest()
        )

    def no_open_sessions():
        assert not any(session.in_transaction() for session in sessions)

    class SummaryAdapter:
        calls = 0
        change_membership = False

        def validate_configuration(self):
            pass

        def fingerprint_for(self, unit):
            return fingerprint(unit.model_dump(mode="json"))

        async def summarize(self, unit):
            no_open_sessions()
            self.calls += 1
            result = SummaryResult(
                summary=" ".join(item.body_text for item in unit.feed_items),
                fingerprint=self.fingerprint_for(unit),
                provider="fake",
                preset="fake",
            )
            if self.change_membership:
                self.change_membership = False
                with Session(engine) as session, session.begin():
                    session.scalar(
                        select(ContentCluster)
                        .where(ContentCluster.id == cluster_id)
                        .with_for_update()
                    )
                    _sync_members(
                        session,
                        cluster_id,
                        ClusterPlan(
                            member_ids=tuple(ids),
                            primary_id=ids[0],
                            exact_ids=(),
                            title="Одно событие",
                        ),
                    )
            return result

    class ImportanceAdapter:
        calls = 0

        def validate_configuration(self):
            pass

        def fingerprint_for(self, value):
            return fingerprint(value.model_dump())

        async def score_importance(self, value):
            no_open_sessions()
            self.calls += 1
            return ImportanceComponents(
                scale_score=2,
                significance_score=2,
                reaction_score=1,
                fingerprint=self.fingerprint_for(value),
                provider="fake",
                model="fake",
            )

    class RelevanceAdapter:
        calls = 0

        def validate_configuration(self):
            pass

        def fingerprint_for(self, value):
            return fingerprint(value.model_dump())

        async def score_relevance(self, value):
            no_open_sessions()
            self.calls += 1
            return RelevanceResult(
                relevance=0.9,
                fingerprint=self.fingerprint_for(value),
                provider="fake",
                model="fake",
            )

    summary, importance, relevance = SummaryAdapter(), ImportanceAdapter(), RelevanceAdapter()
    usecase = ContentUnitAnalysisUseCase(
        kind="cluster",
        session_factory=factory,
        summarization_service=SummarizationService(summary),
        importance_service=ImportanceService(importance),
        relevance_service=RelevanceService(relevance),
    )
    first = asyncio.run(usecase.run(cluster_id, user_id=user_id, interests=["тема"]))
    second = asyncio.run(usecase.run(cluster_id, user_id=user_id, interests=["тема"]))
    assert first.succeeded and second.succeeded
    assert second.summary.reused and second.importance.reused and second.relevance.reused
    assert (summary.calls, importance.calls, relevance.calls) == (1, 1, 1)

    summary.change_membership = True
    third = asyncio.run(usecase.run(cluster_id, user_id=user_id, interests=["тема"], force=True))
    assert third.succeeded and third.attempt_count == 2
    assert (summary.calls, importance.calls, relevance.calls) == (3, 2, 2)
    no_open_sessions()
    with Session(engine) as session:
        cluster = session.get(ContentCluster, cluster_id)
        assert cluster.summary == third.summary.result.summary
        assert cluster.importance_score == third.importance.result.total_score
        assert (
            cluster.ai_metadata["summary"]["stale_candidate"]["result"]["summary"]
            == first.summary.result.summary
        )
        assert all(session.get(FeedItem, item_id).summary is None for item_id in ids)
        score = session.scalar(select(UserItemScore).where(UserItemScore.cluster_id == cluster_id))
        assert score.relevance_score == third.relevance.result.relevance
