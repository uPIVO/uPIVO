import pytest

from app.ai.dto import SummaryResult
from app.ai.repository import FeedItemAnalysisRepository, SaveDisposition
from app.models.content_cluster import ContentCluster
from app.models.feed_item import FeedItem
from app.models.user_item_score import UserItemScore


class _Result:
    def __init__(self, rows):
        self.rows = rows

    def one_or_none(self):
        return self.rows[0] if self.rows else None

    def all(self):
        return self.rows


class _Session:
    def __init__(self):
        self.cluster = ContentCluster(id=7, title="Общее событие", summary=None)
        self.cluster.ai_metadata = None
        self.cluster.importance_score = None
        self.items = [
            FeedItem(
                id=i,
                source_id=1,
                external_id=str(i),
                body_text=f"Факты {i}",
                original_url=f"https://example.test/{i}",
                ai_metadata=None,
            )
            for i in (7, 8)
        ]
        self.membership = {7: 7, 8: 7}
        self.primary = 7
        self.scores = []

    def scalar(self, statement):
        entity = statement.column_descriptions[0].get("entity")
        if entity is ContentCluster:
            return self.cluster
        if entity is UserItemScore:
            return self.scores[0] if self.scores else None
        return self.membership.get(7)

    def execute(self, statement):
        # Кластер загружается набором публикаций с источником и флагом primary.
        if len(statement.column_descriptions) == 3:
            return _Result([(item, "Источник", item.id == self.primary) for item in self.items])
        return _Result([(self.items[0], "Источник")])

    def scalars(self, statement):
        return _Result(self.scores)

    def flush(self):
        pass


def _summary():
    return SummaryResult(
        summary="Все факты события", fingerprint=f"v1:{'a' * 64}", provider="test", preset="test"
    )


def test_cluster_snapshot_contains_all_members_and_primary():
    session = _Session()
    repo = FeedItemAnalysisRepository()
    snapshot = repo.load_snapshot(session, kind="cluster", unit_id=7)
    assert snapshot.unit.kind == "cluster"
    assert [item.feed_item_id for item in snapshot.unit.feed_items] == [7, 8]
    assert snapshot.unit.feed_items[0].is_primary is True
    assert snapshot.summary_result is None

    session.primary = 8
    changed = repo.load_snapshot(session, kind="cluster", unit_id=7)
    assert changed.input_fingerprint != snapshot.input_fingerprint


def test_member_cannot_be_loaded_as_an_independent_unit():
    with pytest.raises(ValueError, match="кластер.*7"):
        FeedItemAnalysisRepository().load_snapshot(_Session(), kind="feed_item", unit_id=7)


def test_cluster_summary_is_saved_once_without_copying_to_members():
    session = _Session()
    repo = FeedItemAnalysisRepository()
    snapshot = repo.load_snapshot(session, kind="cluster", unit_id=7)
    disposition = repo.save_summary(
        session,
        kind="cluster",
        unit_id=7,
        expected_input_fingerprint=snapshot.input_fingerprint,
        result=_summary(),
    )
    assert disposition is SaveDisposition.CURRENT
    assert session.cluster.summary == "Все факты события"
    assert all(item.summary is None and item.ai_metadata is None for item in session.items)


def test_membership_appearing_during_feed_item_call_preserves_only_stale_candidate():
    session = _Session()
    session.membership.clear()
    repo = FeedItemAnalysisRepository()
    snapshot = repo.load_snapshot(session, kind="feed_item", unit_id=7)
    session.membership[7] = 7
    disposition = repo.save_summary(
        session,
        feed_item_id=7,
        expected_input_fingerprint=snapshot.input_fingerprint,
        result=_summary(),
    )
    assert disposition is SaveDisposition.STALE_CANDIDATE
    assert session.items[0].summary is None
    assert (
        session.items[0].ai_metadata["summary"]["stale_candidate"]["result"]["summary"]
        == "Все факты события"
    )
