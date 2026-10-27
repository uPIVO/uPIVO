"""Интеграционные проверки интересов и персональной оценки на отдельной PostgreSQL.

Запускать только с ITD86_TEST_DATABASE_URL на одноразовую БД itd86_interests_test*.
Обычный pytest пропускает эти проверки; внешние AI-провайдеры не вызываются.
"""

import asyncio
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.ai.analysis_metadata import interests_input_fingerprint
from app.ai.dto import RelevanceResult, SummaryResult
from app.ai.repository import ContentUnitAnalysisRepository
from app.models import (
    ClusterItem,
    ContentCluster,
    FeedItem,
    Source,
    SourceType,
    User,
    UserInterest,
    UserItemScore,
    UserSource,
)
from app.services.user_interests import (
    DuplicateInterestError,
    create_interest,
    delete_interest,
    read_interest_texts,
)
from app.services.user_relevance import (
    PostgresAdvisoryLock,
    UserRelevanceService,
    UserRelevanceStatus,
)
from app.services.user_relevance_storage import (
    InterestGuardedRepository,
    UserRelevanceCatalog,
)

_URL = os.environ.get("ITD86_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not _URL, reason="Нужна отдельная тестовая PostgreSQL")
_BACKEND = Path(__file__).resolve().parents[1]


def _alembic(url, revision):
    environment = os.environ.copy()
    environment["DATABASE_URL"] = url.render_as_string(hide_password=False)
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", revision],
        cwd=_BACKEND,
        env=environment,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    return result


@pytest.fixture
def engine():
    parent_url = make_url(_URL)
    assert parent_url.database.startswith("itd86_interests_test"), "Нельзя использовать рабочую БД"
    database_name = f"itd86_interests_test_{uuid4().hex[:12]}"
    admin = create_engine(parent_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{database_name}"'))
    url = parent_url.set(database=database_name)
    _alembic(url, "head")
    database = create_engine(url)
    try:
        yield database
    finally:
        database.dispose()
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE "{database_name}" WITH (FORCE)'))
        admin.dispose()


class _Scoring:
    """Подменяет RelevanceService: считает обращения к «провайдеру» и умеет мутировать данные."""

    def __init__(self, *, on_call: Any = None) -> None:
        self.on_call = on_call
        self.inputs: list[dict[str, Any]] = []
        self.provider_calls = 0

    def validate_configuration(self) -> None:
        return None

    async def score_relevance(self, value, *, prior_result=None, force: bool = False):
        self.inputs.append(dict(value))
        if self.on_call is not None:
            self.on_call(len(self.inputs))
        if prior_result is not None and not force:
            return SimpleNamespace(result=prior_result, reused=True)
        self.provider_calls += 1
        return SimpleNamespace(
            result=RelevanceResult(
                relevance=0.8, fingerprint=f"v1:{'c' * 64}", provider="fake", model="test"
            ),
            reused=False,
        )


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


def _summary(label: str = "Общее содержание события") -> SummaryResult:
    return SummaryResult(summary=label, fingerprint=f"v1:{'a' * 64}", provider="fake", preset="t")


def _seed(session: Session, *, interests: list[str] | None = None, enabled: bool = True):
    token = uuid4().hex
    user = User(email=f"{token}@example.test", password_hash="test-only")
    source = Source(
        type=SourceType.TELEGRAM,
        name="Источник",
        external_id=token,
        canonical_url=f"https://example.test/{token}",
    )
    other = Source(
        type=SourceType.TELEGRAM,
        name="Другой источник",
        external_id=f"{token}-other",
        canonical_url=f"https://example.test/{token}/other",
    )
    session.add_all([user, source, other])
    session.flush()
    session.add(UserSource(user_id=user.id, source_id=source.id, enabled=enabled))
    items = [
        FeedItem(
            source_id=source.id,
            external_id=f"{token}-{index}",
            title=f"Новость {index}",
            body_text=f"Факты {index}",
            original_url=f"https://example.test/{token}/{index}",
        )
        for index in range(2)
    ]
    session.add_all(items)
    session.flush()
    for text_value in interests or []:
        session.add(UserInterest(user_id=user.id, text=text_value))
    session.flush()
    return SimpleNamespace(user=user, source=source, other=other, items=items)


def _save_summary(repository, session: Session, *, kind: str, unit_id: int) -> None:
    snapshot = repository.load_snapshot(session, kind=kind, unit_id=unit_id)
    repository.save_summary(
        session,
        kind=kind,
        unit_id=unit_id,
        expected_input_fingerprint=snapshot.input_fingerprint,
        result=_summary(),
    )


def _service(engine, scoring: _Scoring) -> UserRelevanceService:
    repository = InterestGuardedRepository()
    return UserRelevanceService(
        session_factory=lambda: Session(engine),
        relevance_service_factory=lambda: scoring,
        repository=repository,
        catalog=UserRelevanceCatalog(session_factory=lambda: Session(engine)),
        scheduler=_Scheduler(),
        lock=PostgresAdvisoryLock(engine_factory=lambda: engine),
    )


def test_migration_enforces_uniqueness_not_blank_and_cascade(engine):
    # Проверяются только ограничения user_interests: никаких источников и публикаций,
    # чтобы каскад не зависел от посторонних FK пользователя.
    with Session(engine) as session, session.begin():
        user = User(email=f"{uuid4().hex}@example.test", password_hash="test-only")
        session.add(user)
        session.flush()
        user_id = user.id
        session.add(UserInterest(user_id=user_id, text="OpenAI"))
        session.flush()

    with pytest.raises(IntegrityError):
        with Session(engine) as session, session.begin():
            session.add(UserInterest(user_id=user_id, text=" openai "))

    with pytest.raises(IntegrityError):
        with Session(engine) as session, session.begin():
            session.add(UserInterest(user_id=user_id, text="   "))

    with Session(engine) as session, session.begin():
        session.execute(text("DELETE FROM users WHERE id = :user_id"), {"user_id": user_id})
    with Session(engine) as session:
        assert session.scalars(select(UserInterest)).all() == []


def test_duplicate_interest_is_reported_by_service(engine):
    with Session(engine) as session, session.begin():
        seeded = _seed(session)
        # id читается внутри транзакции: после commit объект был бы detached.
        user_id = seeded.user.id

    with Session(engine) as session:
        create_interest(session, user_id, "  Новые модели OpenAI ")
        assert read_interest_texts(session, user_id) == ["Новые модели OpenAI"]

    with pytest.raises(DuplicateInterestError):
        with Session(engine) as session:
            create_interest(session, user_id, "новые модели openai")


def test_interest_change_invalidates_current_score_but_keeps_history(engine):
    repository = ContentUnitAnalysisRepository()
    scoring = _Scoring()
    with Session(engine) as session, session.begin():
        seeded = _seed(session, interests=["ИИ"])
        user_id = seeded.user.id
        item_id = seeded.items[0].id
        _save_summary(repository, session, kind="feed_item", unit_id=item_id)

    service = _service(engine, scoring)
    first = asyncio.run(service.score_user_content_unit(user_id, "feed_item", item_id))
    assert first.status is UserRelevanceStatus.CURRENT

    with Session(engine) as session:
        create_interest(session, user_id, "Электромобили")

    with Session(engine) as session:
        row = session.scalar(
            select(UserItemScore).where(
                UserItemScore.user_id == user_id, UserItemScore.feed_item_id == item_id
            )
        )
        assert row.relevance_score is None
        history = row.analysis_metadata["relevance"]
        assert history["last_good"]["status"] == "stale"
        assert history["last_good"]["result"]["relevance"] == 0.8

    second = asyncio.run(service.score_user_content_unit(user_id, "feed_item", item_id))
    assert second.status is UserRelevanceStatus.CURRENT
    assert second.reused is False

    with Session(engine) as session:
        row = session.scalar(
            select(UserItemScore).where(
                UserItemScore.user_id == user_id, UserItemScore.feed_item_id == item_id
            )
        )
        assert row.relevance_score == 0.8
        assert row.analysis_metadata["relevance"]["last_good"]["interests_fingerprint"]


def test_missing_summary_or_disabled_source_skips_without_provider_call(engine):
    repository = ContentUnitAnalysisRepository()
    scoring = _Scoring()
    with Session(engine) as session, session.begin():
        seeded = _seed(session, interests=["ИИ"], enabled=False)
        user_id = seeded.user.id
        item_id = seeded.items[0].id

    service = _service(engine, scoring)

    no_summary = asyncio.run(service.score_user_content_unit(user_id, "feed_item", item_id))
    assert no_summary.status is UserRelevanceStatus.SKIPPED
    assert scoring.provider_calls == 0

    with Session(engine) as session, session.begin():
        _save_summary(repository, session, kind="feed_item", unit_id=item_id)

    disabled = asyncio.run(service.score_user_content_unit(user_id, "feed_item", item_id))
    assert disabled.status is UserRelevanceStatus.SKIPPED
    assert scoring.provider_calls == 0


def test_cluster_is_scored_once_when_any_member_source_is_enabled(engine):
    repository = ContentUnitAnalysisRepository()
    scoring = _Scoring()
    with Session(engine) as session, session.begin():
        seeded = _seed(session, interests=["ИИ"])
        extra = FeedItem(
            source_id=seeded.other.id,
            external_id=f"{uuid4().hex}-other",
            title="Чужой источник",
            body_text="Другие факты",
            original_url=f"https://example.test/{uuid4().hex}/other",
        )
        session.add(extra)
        session.flush()
        cluster = ContentCluster(title="Событие")
        session.add(cluster)
        session.flush()
        session.add_all(
            [
                ClusterItem(
                    cluster_id=cluster.id, feed_item_id=seeded.items[0].id, is_primary=True
                ),
                ClusterItem(cluster_id=cluster.id, feed_item_id=seeded.items[1].id),
                ClusterItem(cluster_id=cluster.id, feed_item_id=extra.id),
            ]
        )
        session.flush()
        user_id = seeded.user.id
        member_id = seeded.items[0].id
        cluster_id = cluster.id
        _save_summary(repository, session, kind="cluster", unit_id=cluster_id)

    service = _service(engine, scoring)
    outcome = asyncio.run(service.score_user_content_unit(user_id, "feed_item", member_id))

    assert outcome.kind == "cluster"
    assert outcome.unit_id == cluster_id
    assert outcome.status is UserRelevanceStatus.CURRENT
    assert len(scoring.inputs) == 1

    with Session(engine) as session:
        rows = session.scalars(
            select(UserItemScore).where(UserItemScore.user_id == user_id)
        ).all()
    assert [(row.feed_item_id, row.cluster_id) for row in rows] == [(None, cluster_id)]


def test_second_call_with_same_inputs_reuses_actual_result(engine):
    repository = ContentUnitAnalysisRepository()
    scoring = _Scoring()
    with Session(engine) as session, session.begin():
        seeded = _seed(session, interests=["ИИ"])
        user_id = seeded.user.id
        item_id = seeded.items[0].id
        _save_summary(repository, session, kind="feed_item", unit_id=item_id)

    service = _service(engine, scoring)
    first = asyncio.run(service.score_user_content_unit(user_id, "feed_item", item_id))
    second = asyncio.run(service.score_user_content_unit(user_id, "feed_item", item_id))

    assert first.status is UserRelevanceStatus.CURRENT
    assert second.status is UserRelevanceStatus.CURRENT
    assert scoring.provider_calls == 1
    assert second.reused is True


def test_interests_changed_during_ai_call_do_not_commit_stale_answer(engine):
    repository = ContentUnitAnalysisRepository()
    with Session(engine) as session, session.begin():
        seeded = _seed(session, interests=["ИИ"])
        user_id = seeded.user.id
        item_id = seeded.items[0].id
        _save_summary(repository, session, kind="feed_item", unit_id=item_id)

    def mutate_interests(call: int) -> None:
        if call != 1:
            return
        with Session(engine) as session:
            create_interest(session, user_id, "Электромобили")

    scoring = _Scoring(on_call=mutate_interests)
    service = _service(engine, scoring)

    outcome = asyncio.run(service.score_user_content_unit(user_id, "feed_item", item_id))

    assert outcome.status is UserRelevanceStatus.CURRENT
    # Внутренний повтор use case отключён в DB-пути: гонка стоит два AI-вызова.
    assert scoring.provider_calls == 2
    assert [entry["interests"] for entry in scoring.inputs] == [
        ["ИИ"],
        ["ИИ", "Электромобили"],
    ]
    with Session(engine) as session:
        row = session.scalar(select(UserItemScore).where(UserItemScore.user_id == user_id))
        stored = row.analysis_metadata["relevance"]["last_good"]
        assert stored["interests_fingerprint"] == interests_input_fingerprint(
            read_interest_texts(session, user_id)
        )
        assert row.relevance_score == 0.8


def test_advisory_lock_excludes_parallel_unit_evaluation(engine):
    lock = PostgresAdvisoryLock(engine_factory=lambda: engine)

    with lock.hold(user_id=7, kind="feed_item", unit_id=11) as acquired:
        assert acquired is True
        with lock.hold(user_id=7, kind="feed_item", unit_id=11) as second:
            assert second is False

    with lock.hold(user_id=7, kind="feed_item", unit_id=11) as released_again:
        assert released_again is True

    with lock.hold(user_id=8, kind="feed_item", unit_id=11) as other_user:
        assert other_user is True


def test_interest_removal_keeps_saved_result_as_history(engine):
    with Session(engine) as session, session.begin():
        seeded = _seed(session, interests=["ИИ"])
        user_id = seeded.user.id
        interest = session.scalars(
            select(UserInterest).where(UserInterest.user_id == user_id)
        ).one()
        interest_id = interest.id
        session.add(
            UserItemScore(
                user_id=user_id,
                feed_item_id=seeded.items[0].id,
                relevance_score=0.7,
                analysis_metadata=None,
            )
        )

    with Session(engine) as session:
        delete_interest(session, user_id, interest_id)

    with Session(engine) as session:
        assert read_interest_texts(session, user_id) == []
        row = session.scalar(select(UserItemScore).where(UserItemScore.user_id == user_id))
        assert row.relevance_score is None
