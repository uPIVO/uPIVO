from types import SimpleNamespace

from app.ai.repository import ContentUnitAnalysisRepository
from app.clustering.cluster import ClusterPlan
from app.clustering.persist import _sync_members
from app.models.content_cluster import ClusterItem


class _Rows:
    def __init__(self, rows):
        self.rows = rows

    def all(self):
        return self.rows


class _Session:
    def __init__(self):
        self.links = [
            ClusterItem(cluster_id=10, feed_item_id=1, is_primary=True),
            ClusterItem(cluster_id=10, feed_item_id=2, is_primary=False),
        ]
        self.statements = []
        self.added = []

    def scalars(self, statement):
        self.statements.append(statement)
        if statement.column_descriptions[0].get("entity") is ClusterItem:
            return _Rows(self.links)
        return _Rows([SimpleNamespace(id=i) for i in (1, 2, 3)])

    def add(self, row):
        self.added.append(row)

    def delete(self, row):
        pass

    def flush(self):
        pass


def test_membership_change_invalidates_cluster_and_only_new_singletons(monkeypatch):
    invalidated = []
    monkeypatch.setattr(
        ContentUnitAnalysisRepository,
        "invalidate_target",
        lambda self, session, *, kind, unit_id: invalidated.append((kind, unit_id)),
    )
    session = _Session()
    plan = ClusterPlan(member_ids=(1, 2, 3), primary_id=1, exact_ids=(), title="Событие")
    _sync_members(session, 10, plan)
    assert invalidated == [("cluster", 10), ("feed_item", 3)]
    assert any("FOR UPDATE" in str(statement) for statement in session.statements)


def test_unchanged_membership_does_not_discard_current_cluster_result(monkeypatch):
    invalidated = []
    monkeypatch.setattr(
        ContentUnitAnalysisRepository,
        "invalidate_target",
        lambda self, session, *, kind, unit_id: invalidated.append((kind, unit_id)),
    )
    session = _Session()
    plan = ClusterPlan(member_ids=(1, 2), primary_id=1, exact_ids=(), title="Событие")
    _sync_members(session, 10, plan)
    assert invalidated == []
