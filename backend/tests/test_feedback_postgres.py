"""Интеграционные проверки обратной связи на отдельной PostgreSQL.

Запускать только с ITD89_TEST_DATABASE_URL на одноразовую БД itd89_feedback_test*.
Обычный pytest пропускает эти проверки; рабочая база не используется.
URL должен использовать драйвер psycopg, например
`postgresql+psycopg://postgres@/itd89_feedback_test?host=/tmp/itd89_pg&port=55489`.
"""

import os
import subprocess
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, event, inspect, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    ClusterItem,
    ContentCluster,
    Feedback,
    FeedItem,
    ProcessingStatus,
    Source,
    SourceType,
    User,
    UserSource,
)
from app.models.enums import FeedbackType
from app.schemas.feedback import FeedbackKind
from app.services.feedback import (
    UnavailableFeedbackTargetError,
    delete_feedback,
    get_feedback,
    list_feedback,
    read_reactions,
    set_feedback,
)

_URL = os.environ.get("ITD89_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not _URL, reason="Нужна отдельная тестовая PostgreSQL")
_BACKEND = Path(__file__).resolve().parents[1]


def _alembic(url: str, *arguments: str) -> str:
    environment = os.environ.copy()
    environment["DATABASE_URL"] = url
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *arguments],
        cwd=_BACKEND,
        env=environment,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


@pytest.fixture
def database():
    parent_url = make_url(_URL)
    assert parent_url.database.startswith("itd89_feedback_test"), "Нельзя использовать рабочую БД"
    database_name = f"itd89_feedback_test_{uuid4().hex[:12]}"
    admin = create_engine(parent_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{database_name}"'))
    url = parent_url.set(database=database_name).render_as_string(hide_password=False)
    _alembic(url, "upgrade", "head")
    engine = create_engine(url)
    try:
        yield SimpleNamespace(engine=engine, url=url)
    finally:
        engine.dispose()
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE "{database_name}" WITH (FORCE)'))
        admin.dispose()


def _user(session: Session) -> User:
    user = User(email=f"{uuid4().hex}@example.test", password_hash="test-only")
    session.add(user)
    session.flush()
    return user


def _source(session: Session, *subscribers: User, enabled: bool = True) -> Source:
    token = uuid4().hex
    source = Source(
        type=SourceType.TELEGRAM,
        name=f"Источник {token[:8]}",
        external_id=token,
        canonical_url=f"https://example.test/{token}",
    )
    session.add(source)
    session.flush()
    for subscriber in subscribers:
        session.add(UserSource(user_id=subscriber.id, source_id=source.id, enabled=enabled))
    session.flush()
    return source


def _item(
    session: Session, source: Source, status: ProcessingStatus = ProcessingStatus.NORMALIZED
) -> FeedItem:
    token = uuid4().hex
    item = FeedItem(
        source_id=source.id,
        external_id=token,
        title="Новость",
        body_text="Текст публикации",
        original_url=f"https://example.test/{token}",
        processing_status=status,
    )
    session.add(item)
    session.flush()
    return item


def _cluster(session: Session, items: list[FeedItem]) -> ContentCluster:
    cluster = ContentCluster(title="Инфоповод", summary="Сводка инфоповода")
    session.add(cluster)
    session.flush()
    for index, item in enumerate(items):
        session.add(
            ClusterItem(cluster_id=cluster.id, feed_item_id=item.id, is_primary=index == 0)
        )
    session.flush()
    return cluster


def _run(targets: list[Callable[[], None]]) -> list[Exception]:
    """Запускает сценарии в потоках и возвращает необработанные ошибки."""
    errors: list[Exception] = []

    def wrap(target: Callable[[], None]) -> Callable[[], None]:
        def run() -> None:
            try:
                target()
            except Exception as exc:  # ошибку проверяет сам тест
                errors.append(exc)

        return run

    threads = [threading.Thread(target=wrap(target)) for target in targets]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert not any(thread.is_alive() for thread in threads), "Сценарий не завершился"
    return errors


def _table_exists(url: str, name: str) -> bool:
    engine = create_engine(url)
    try:
        return inspect(engine).has_table(name)
    finally:
        engine.dispose()


def _enum_labels(url: str, name: str) -> list[str]:
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            return list(
                connection.execute(
                    text(
                        "SELECT e.enumlabel FROM pg_enum e "
                        "JOIN pg_type t ON t.oid = e.enumtypid "
                        "WHERE t.typname = :name ORDER BY e.enumsortorder"
                    ),
                    {"name": name},
                ).scalars()
            )
    finally:
        engine.dispose()


def _constraint_names(url: str, table: str) -> dict[str, set[str]]:
    engine = create_engine(url)
    try:
        inspector = inspect(engine)
        return {
            "check": {item["name"] for item in inspector.get_check_constraints(table)},
            "unique": {item["name"] for item in inspector.get_unique_constraints(table)},
            "foreign_key": {item["name"] for item in inspector.get_foreign_keys(table)},
        }
    finally:
        engine.dispose()


def test_migration_upgrade_downgrade_reupgrade(database):
    url = database.url
    database.engine.dispose()

    names = _constraint_names(url, "feedback")
    assert "ck_feedback_exactly_one_target" in names["check"]
    assert {"uq_feedback_user_feed_item", "uq_feedback_user_cluster"} <= names["unique"]
    assert {
        "fk_feedback_user_id_users",
        "fk_feedback_feed_item_id_feed_items",
        "fk_feedback_cluster_id_content_clusters",
    } <= names["foreign_key"]
    assert _enum_labels(url, "feedback_type") == ["useful", "not_interested"]

    _alembic(url, "downgrade", "-1")
    assert not _table_exists(url, "feedback")
    assert _enum_labels(url, "feedback_type") == []

    _alembic(url, "upgrade", "head")
    assert _table_exists(url, "feedback")
    assert _enum_labels(url, "feedback_type") == ["useful", "not_interested"]

    engine = create_engine(url)
    try:
        with Session(engine) as session:
            user = _user(session)
            source = _source(session, user)
            item = _item(session, source)
            user_id, item_id = user.id, item.id
            session.commit()
            feedback, created = set_feedback(
                session, user_id, FeedbackKind.FEED_ITEM, item_id, FeedbackType.USEFUL
            )
        assert created is True
        assert feedback.kind == FeedbackKind.FEED_ITEM
        assert feedback.unit_id == item_id
    finally:
        engine.dispose()


def test_check_xor_and_enum_are_enforced_by_database(database):
    engine = database.engine
    with Session(engine) as session:
        user = _user(session)
        source = _source(session, user)
        item = _item(session, source)
        cluster = _cluster(session, [item])
        user_id, item_id, cluster_id = user.id, item.id, cluster.id
        session.commit()

        with pytest.raises(IntegrityError):
            session.execute(
                text("INSERT INTO feedback (user_id, reaction) VALUES (:user_id, 'useful')"),
                {"user_id": user_id},
            )
        session.rollback()

        with pytest.raises(IntegrityError):
            session.execute(
                text(
                    "INSERT INTO feedback (user_id, feed_item_id, cluster_id, reaction) "
                    "VALUES (:user_id, :item_id, :cluster_id, 'useful')"
                ),
                {"user_id": user_id, "item_id": item_id, "cluster_id": cluster_id},
            )
        session.rollback()

        with pytest.raises(DBAPIError) as invalid_reaction:
            session.execute(
                text(
                    "INSERT INTO feedback (user_id, cluster_id, reaction) "
                    "VALUES (:user_id, :cluster_id, 'neutral')"
                ),
                {"user_id": user_id, "cluster_id": cluster_id},
            )
        assert "feedback_type" in str(invalid_reaction.value)
        session.rollback()


def test_unique_pair_and_cascade_delete(database):
    engine = database.engine
    with Session(engine) as session:
        user = _user(session)
        other = _user(session)
        source = _source(session, user, other)
        item = _item(session, source)
        member = _item(session, source)
        cluster = _cluster(session, [member])
        user_id, other_id = user.id, other.id
        item_id, cluster_id = item.id, cluster.id
        session.commit()

        set_feedback(session, user_id, FeedbackKind.FEED_ITEM, item_id, FeedbackType.USEFUL)
        set_feedback(session, user_id, FeedbackKind.CLUSTER, cluster_id, FeedbackType.USEFUL)
        set_feedback(
            session, other_id, FeedbackKind.FEED_ITEM, item_id, FeedbackType.NOT_INTERESTED
        )
        assert set_feedback(
            session, other_id, FeedbackKind.CLUSTER, cluster_id, FeedbackType.USEFUL
        )[1]

        with pytest.raises(IntegrityError):
            session.execute(
                text(
                    "INSERT INTO feedback (user_id, feed_item_id, reaction) "
                    "VALUES (:user_id, :item_id, 'useful')"
                ),
                {"user_id": user_id, "item_id": item_id},
            )
        session.rollback()

        with pytest.raises(IntegrityError):
            session.execute(
                text(
                    "INSERT INTO feedback (user_id, cluster_id, reaction) "
                    "VALUES (:user_id, :cluster_id, 'useful')"
                ),
                {"user_id": user_id, "cluster_id": cluster_id},
            )
        session.rollback()

        # Каскад от публикации убирает реакции на неё, но не на инфоповод.
        session.execute(text("DELETE FROM feed_items WHERE id = :id"), {"id": item_id})
        session.commit()
        assert (
            session.scalar(select(Feedback.id).where(Feedback.feed_item_id.is_not(None))) is None
        )
        assert (
            session.scalar(select(Feedback.id).where(Feedback.cluster_id == cluster_id))
            is not None
        )

        # Каскад от инфоповода убирает оставшиеся реакции.
        session.execute(
            text("DELETE FROM cluster_items WHERE cluster_id = :id"), {"id": cluster_id}
        )
        session.execute(text("DELETE FROM content_clusters WHERE id = :id"), {"id": cluster_id})
        session.commit()
        assert session.scalar(select(Feedback.id)) is None


def test_cascade_delete_from_user(database):
    engine = database.engine
    with Session(engine) as session:
        user = _user(session)
        source = _source(session, user)
        item = _item(session, source)
        member = _item(session, source)
        cluster = _cluster(session, [member])
        user_id, item_id, cluster_id = user.id, item.id, cluster.id
        session.commit()

        set_feedback(session, user_id, FeedbackKind.FEED_ITEM, item_id, FeedbackType.USEFUL)
        set_feedback(session, user_id, FeedbackKind.CLUSTER, cluster_id, FeedbackType.USEFUL)
        assert len(list(session.scalars(select(Feedback.id)))) == 2

        # Сначала убираем подписки: их FK на users не каскадный.
        session.execute(text("DELETE FROM user_sources WHERE user_id = :id"), {"id": user_id})
        session.execute(text("DELETE FROM users WHERE id = :id"), {"id": user_id})
        session.commit()
        assert session.scalar(select(Feedback.id)) is None


def test_concurrent_put_keeps_single_row(database):
    engine = database.engine
    with Session(engine) as session:
        user = _user(session)
        source = _source(session, user)
        item = _item(session, source)
        user_id, item_id = user.id, item.id
        session.commit()

    barrier = threading.Barrier(2)
    results: list[tuple[Any, bool]] = []

    def writer(reaction: FeedbackType) -> Callable[[], None]:
        def run() -> None:
            with Session(engine) as session:
                barrier.wait(timeout=60)
                results.append(
                    set_feedback(session, user_id, FeedbackKind.FEED_ITEM, item_id, reaction)
                )

        return run

    errors = _run([writer(FeedbackType.USEFUL), writer(FeedbackType.NOT_INTERESTED)])
    assert errors == []
    assert sorted(created for _, created in results) == [False, True]

    with Session(engine) as session:
        rows = list(session.scalars(select(Feedback).where(Feedback.user_id == user_id)))
        assert len(rows) == 1
        assert rows[0].reaction in (FeedbackType.USEFUL, FeedbackType.NOT_INTERESTED)


def test_concurrent_put_and_delete_keep_at_most_one_row(database):
    engine = database.engine
    with Session(engine) as session:
        user = _user(session)
        source = _source(session, user)
        item = _item(session, source)
        user_id, item_id = user.id, item.id
        session.commit()
        set_feedback(session, user_id, FeedbackKind.FEED_ITEM, item_id, FeedbackType.USEFUL)

    barrier = threading.Barrier(2)

    def put() -> None:
        with Session(engine) as session:
            barrier.wait(timeout=60)
            set_feedback(
                session, user_id, FeedbackKind.FEED_ITEM, item_id, FeedbackType.NOT_INTERESTED
            )

    def remove() -> None:
        with Session(engine) as session:
            barrier.wait(timeout=60)
            delete_feedback(session, user_id, FeedbackKind.FEED_ITEM, item_id)

    assert _run([put, remove]) == []
    with Session(engine) as session:
        rows = list(session.scalars(select(Feedback).where(Feedback.user_id == user_id)))
    assert len(rows) <= 1


def test_availability_and_isolation_on_postgres(database):
    engine = database.engine
    with Session(engine) as session:
        user = _user(session)
        other = _user(session)
        enabled = _source(session, user, other)
        disabled = _source(session, user, enabled=False)
        foreign = _source(session, other)

        standalone = _item(session, enabled)
        member = _item(session, enabled)
        mixed_foreign = _item(session, foreign)
        mixed_own = _item(session, enabled)
        hidden = _item(session, disabled)
        pending = _item(session, enabled, ProcessingStatus.PENDING)
        cluster = _cluster(session, [member])
        mixed_cluster = _cluster(session, [mixed_foreign, mixed_own])
        hidden_cluster = _cluster(session, [hidden])
        ids = SimpleNamespace(
            user=user.id,
            other=other.id,
            standalone=standalone.id,
            member=member.id,
            hidden=hidden.id,
            pending=pending.id,
            cluster=cluster.id,
            mixed_cluster=mixed_cluster.id,
            hidden_cluster=hidden_cluster.id,
        )
        session.commit()

        assert set_feedback(
            session, ids.user, FeedbackKind.FEED_ITEM, ids.standalone, FeedbackType.USEFUL
        )[1]
        assert set_feedback(
            session, ids.user, FeedbackKind.CLUSTER, ids.mixed_cluster, FeedbackType.NOT_INTERESTED
        )[1]
        with pytest.raises(UnavailableFeedbackTargetError):
            set_feedback(
                session, ids.user, FeedbackKind.FEED_ITEM, ids.member, FeedbackType.USEFUL
            )
        with pytest.raises(UnavailableFeedbackTargetError):
            set_feedback(
                session, ids.user, FeedbackKind.FEED_ITEM, ids.hidden, FeedbackType.USEFUL
            )
        with pytest.raises(UnavailableFeedbackTargetError):
            set_feedback(
                session, ids.user, FeedbackKind.FEED_ITEM, ids.pending, FeedbackType.USEFUL
            )
        with pytest.raises(UnavailableFeedbackTargetError):
            set_feedback(
                session, ids.user, FeedbackKind.CLUSTER, ids.hidden_cluster, FeedbackType.USEFUL
            )
        with pytest.raises(UnavailableFeedbackTargetError):
            set_feedback(
                session, ids.user, FeedbackKind.CLUSTER, ids.cluster + 1000, FeedbackType.USEFUL
            )

        # Реакции разных пользователей независимы и не пересекаются в выдаче.
        set_feedback(
            session, ids.other, FeedbackKind.FEED_ITEM, ids.standalone, FeedbackType.NOT_INTERESTED
        )
        assert (
            get_feedback(session, ids.user, FeedbackKind.FEED_ITEM, ids.standalone).reaction
            == FeedbackType.USEFUL
        )
        assert {item.user_id for item in list_feedback(session, ids.user)} == {ids.user}

        # Отключение источника не мешает читать и удалять свою прежнюю реакцию.
        session.execute(
            text("UPDATE user_sources SET enabled = false WHERE user_id = :id"),
            {"id": ids.user},
        )
        session.commit()
        assert (
            get_feedback(session, ids.user, FeedbackKind.FEED_ITEM, ids.standalone).reaction
            == FeedbackType.USEFUL
        )
        with pytest.raises(UnavailableFeedbackTargetError):
            set_feedback(
                session,
                ids.user,
                FeedbackKind.FEED_ITEM,
                ids.standalone,
                FeedbackType.NOT_INTERESTED,
            )
        session.rollback()
        delete_feedback(session, ids.user, FeedbackKind.FEED_ITEM, ids.standalone)
        delete_feedback(session, ids.user, FeedbackKind.FEED_ITEM, ids.standalone)


def test_batch_reads_use_single_query(database):
    engine = database.engine
    with Session(engine) as session:
        user = _user(session)
        source = _source(session, user)
        first = _item(session, source)
        second = _item(session, source)
        cluster = _cluster(session, [second])
        user_id, first_id, second_id, cluster_id = user.id, first.id, second.id, cluster.id
        session.commit()

        set_feedback(session, user_id, FeedbackKind.FEED_ITEM, first_id, FeedbackType.USEFUL)
        set_feedback(
            session, user_id, FeedbackKind.CLUSTER, cluster_id, FeedbackType.NOT_INTERESTED
        )

        statements: list[str] = []

        def record(conn, cursor, statement, parameters, context, executemany):
            statements.append(statement)

        event.listen(engine, "before_cursor_execute", record)
        try:
            found = read_reactions(
                session,
                user_id,
                [
                    (FeedbackKind.FEED_ITEM, first_id),
                    (FeedbackKind.CLUSTER, cluster_id),
                    (FeedbackKind.FEED_ITEM, second_id),
                ],
            )
            assert found == {
                (FeedbackKind.FEED_ITEM, first_id): FeedbackType.USEFUL,
                (FeedbackKind.CLUSTER, cluster_id): FeedbackType.NOT_INTERESTED,
            }
            assert len(statements) == 1

            statements.clear()
            listed = list_feedback(
                session, user_id, kind=FeedbackKind.FEED_ITEM, unit_ids=[first_id, second_id]
            )
            assert [item.unit_id for item in listed] == [first_id]
            # Один запрос на реакции плюс проверка пользователя в require_user.
            assert len(statements) == 2
        finally:
            event.remove(engine, "before_cursor_execute", record)
