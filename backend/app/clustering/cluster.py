"""Group feed items into stories. Does not delete publications."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime

from app.clustering.similarity import BLOCK_TOKEN_MAX_DOCS, texts_match, token_set
from app.processing.normalize import content_hash
from app.processing.text import clean_oneline, clean_text


@dataclass(frozen=True)
class StoryItem:
    id: int
    title: str | None
    body_text: str
    content_hash: str | None
    published_at: datetime | None


@dataclass(frozen=True)
class ClusterPlan:
    """One story of at least two publications."""

    member_ids: tuple[int, ...]
    primary_id: int
    exact_ids: frozenset[int]
    title: str | None


@dataclass(frozen=True)
class StoredCluster:
    """Cluster row already in the database, members as (feed_item_id, is_primary)."""

    id: int
    title: str | None
    summary: str | None
    members: tuple[tuple[int, bool], ...]


@dataclass(frozen=True)
class AssignedCluster:
    """Plan matched to an existing cluster id, or None when a new row is needed."""

    cluster_id: int | None
    summary: str | None
    plan: ClusterPlan


@dataclass(frozen=True)
class MemberView:
    feed_item_id: int
    role: str
    kind: str
    title: str | None


@dataclass(frozen=True)
class ClusterView:
    cluster_id: int | None
    title: str | None
    primary_id: int
    members: tuple[MemberView, ...]


def build_plans(items: list[StoryItem]) -> list[ClusterPlan]:
    """Cluster exact hashes and similar title/body pairs.

    A single publication is not a cluster. Summary and importance are not read.
    """
    if len({item.id for item in items}) != len(items):
        raise ValueError("duplicate feed item id")
    if len(items) < 2:
        return []

    parent = {item.id: item.id for item in items}

    def find(item_id: int) -> int:
        while parent[item_id] != item_id:
            parent[item_id] = parent[parent[item_id]]
            item_id = parent[item_id]
        return item_id

    def union(left: int, right: int) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root == right_root:
            return
        if left_root < right_root:
            parent[right_root] = left_root
        else:
            parent[left_root] = right_root

    by_id = {item.id: item for item in items}
    stored_buckets: dict[str, list[int]] = defaultdict(list)
    text_buckets: dict[str, list[int]] = defaultdict(list)
    for item in items:
        stored = (item.content_hash or "").strip()
        if stored:
            stored_buckets[stored].append(item.id)
        text_buckets[_text_digest(item)].append(item.id)
    for bucket in list(stored_buckets.values()) + list(text_buckets.values()):
        if len(bucket) < 2:
            continue
        head = bucket[0]
        for other in bucket[1:]:
            union(head, other)

    for left_id, right_id in _candidate_pairs(items):
        if find(left_id) == find(right_id):
            continue
        left = by_id[left_id]
        right = by_id[right_id]
        if texts_match(left.title, left.body_text, right.title, right.body_text):
            union(left_id, right_id)

    grouped: dict[int, list[StoryItem]] = defaultdict(list)
    for item in items:
        grouped[find(item.id)].append(item)

    plans: list[ClusterPlan] = []
    for group in grouped.values():
        if len(group) < 2:
            continue
        primary = _choose_primary(group)
        plans.append(
            ClusterPlan(
                member_ids=tuple(sorted(item.id for item in group)),
                primary_id=primary.id,
                exact_ids=_exact_ids(group),
                title=clean_oneline(primary.title),
            )
        )
    plans.sort(key=lambda plan: plan.member_ids)
    return plans


def assign_clusters(
    existing: list[StoredCluster], plans: list[ClusterPlan]
) -> list[AssignedCluster]:
    """Reuse the cluster that already holds the most members of each plan.

    Each existing id is used at most once. Unmatched plans need a new row.
    Existing summaries are returned unchanged so a re-run does not wipe them.
    """
    pairs: list[tuple[int, int, int, int, int]] = []
    for index, plan in enumerate(plans):
        wanted = set(plan.member_ids)
        for cluster in existing:
            have = {feed_item_id for feed_item_id, _primary in cluster.members}
            overlap = len(wanted & have)
            if overlap:
                pairs.append((overlap, -cluster.id, -index, cluster.id, index))
    pairs.sort(reverse=True)

    used_plans: set[int] = set()
    used_clusters: set[int] = set()
    chosen: dict[int, int] = {}
    for _overlap, _neg_id, _neg_index, cluster_id, index in pairs:
        if index in used_plans or cluster_id in used_clusters:
            continue
        used_plans.add(index)
        used_clusters.add(cluster_id)
        chosen[index] = cluster_id

    by_id = {cluster.id: cluster for cluster in existing}
    assigned: list[AssignedCluster] = []
    for index, plan in enumerate(plans):
        cluster_id = chosen.get(index)
        summary = by_id[cluster_id].summary if cluster_id is not None else None
        assigned.append(AssignedCluster(cluster_id=cluster_id, summary=summary, plan=plan))
    return assigned


def stale_cluster_ids(existing: list[StoredCluster], assigned: list[AssignedCluster]) -> list[int]:
    """Cluster ids that this run no longer uses. Publications are not included."""
    keep = {slot.cluster_id for slot in assigned if slot.cluster_id is not None}
    return sorted(cluster.id for cluster in existing if cluster.id not in keep)


def cluster_views(
    plans: list[ClusterPlan],
    items: list[StoryItem],
    cluster_ids: list[int | None] | None = None,
) -> list[ClusterView]:
    by_id = {item.id: item for item in items}
    views: list[ClusterView] = []
    for index, plan in enumerate(plans):
        cluster_id = None if cluster_ids is None else cluster_ids[index]
        ordered = (
            plan.primary_id,
            *[item_id for item_id in plan.member_ids if item_id != plan.primary_id],
        )
        members: list[MemberView] = []
        for feed_item_id in ordered:
            item = by_id[feed_item_id]
            members.append(
                MemberView(
                    feed_item_id=feed_item_id,
                    role="primary" if feed_item_id == plan.primary_id else "member",
                    kind="exact" if feed_item_id in plan.exact_ids else "similar",
                    title=clean_oneline(item.title),
                )
            )
        views.append(
            ClusterView(
                cluster_id=cluster_id,
                title=plan.title,
                primary_id=plan.primary_id,
                members=tuple(members),
            )
        )
    return views


def unclustered_count(plans: list[ClusterPlan], items: list[StoryItem]) -> int:
    covered = sum(len(plan.member_ids) for plan in plans)
    return len(items) - covered


def format_report(views: list[ClusterView], *, unclustered: int, dry_run: bool) -> str:
    lines: list[str] = []
    new_index = 0
    for view in views:
        if view.cluster_id is None:
            new_index += 1
            label = f"new-{new_index}"
        else:
            label = str(view.cluster_id)
        lines.append(f"cluster={label}\tprimary={view.primary_id}\ttitle={view.title or ''}")
        for member in view.members:
            lines.append(
                f"{member.feed_item_id}\t{member.role}\t{member.kind}\t{member.title or ''}"
            )
    suffix = " dry-run" if dry_run else ""
    lines.append(f"clusters={len(views)} unclustered={unclustered}{suffix}")
    return "\n".join(lines)


def _choose_primary(group: list[StoryItem]) -> StoryItem:
    """Longest cleaned body, then newest published_at, then smallest id."""

    def sort_key(item: StoryItem) -> tuple[int, datetime, int]:
        published = item.published_at or datetime.min
        if published.tzinfo is not None:
            published = published.replace(tzinfo=None)
        return (len(clean_text(item.body_text)), published, -item.id)

    return max(group, key=sort_key)


def _exact_ids(group: list[StoryItem]) -> frozenset[int]:
    stored: dict[str, list[int]] = defaultdict(list)
    texts: dict[str, list[int]] = defaultdict(list)
    for item in group:
        key = (item.content_hash or "").strip()
        if key:
            stored[key].append(item.id)
        texts[_text_digest(item)].append(item.id)
    exact: list[int] = []
    for bucket in list(stored.values()) + list(texts.values()):
        if len(bucket) > 1:
            exact.extend(bucket)
    return frozenset(exact)


def _text_digest(item: StoryItem) -> str:
    return content_hash(clean_oneline(item.title), clean_text(item.body_text))


def _candidate_pairs(items: list[StoryItem]) -> set[tuple[int, int]]:
    postings: dict[str, list[int]] = defaultdict(list)
    for item in items:
        for token in token_set(item.title) | token_set(item.body_text):
            postings[token].append(item.id)
    pairs: set[tuple[int, int]] = set()
    for ids in postings.values():
        unique = sorted(set(ids))
        if len(unique) < 2 or len(unique) > BLOCK_TOKEN_MAX_DOCS:
            continue
        for index, left in enumerate(unique):
            for right in unique[index + 1 :]:
                pairs.add((left, right))
    return pairs
