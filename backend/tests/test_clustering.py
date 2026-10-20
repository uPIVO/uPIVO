from datetime import datetime
from pathlib import Path

from app.clustering.__main__ import main
from app.clustering.cluster import (
    StoredCluster,
    StoryItem,
    assign_clusters,
    build_plans,
    cluster_views,
    format_report,
    stale_cluster_ids,
    unclustered_count,
)
from app.clustering.persist import _sync_members
from app.clustering.similarity import texts_match
from app.models.content_cluster import ClusterItem

FIXTURE = Path(__file__).parent / "fixtures" / "cluster" / "sample.json"

ARTEMIS_A = "NASA landed the Artemis crew on the Moon after a four-day flight"
ARTEMIS_B = "Artemis crew has landed on the Moon, NASA confirms the four-day mission"
APPLE = "Apple unveiled the iPhone 17 with a new camera"
PYTHON = "The Python Software Foundation released Python 3.14"


def _story(
    item_id: int,
    title: str | None,
    body: str = "",
    *,
    content_hash: str | None = None,
    published_at: datetime | None = None,
) -> StoryItem:
    return StoryItem(
        id=item_id,
        title=title,
        body_text=body,
        content_hash=content_hash,
        published_at=published_at,
    )


def test_exact_hash_groups_different_text():
    plans = build_plans(
        [
            _story(1, "Alpha launch notes", "one story about a rocket", content_hash="same"),
            _story(2, "Pasta water and salt", "boil then drain", content_hash="same"),
        ]
    )
    assert len(plans) == 1
    assert plans[0].member_ids == (1, 2)
    assert plans[0].exact_ids == frozenset({1, 2})


def test_identical_text_without_hash_is_exact():
    plans = build_plans(
        [
            _story(1, "Same headline", "same body"),
            _story(2, "Same headline", "same body"),
        ]
    )
    assert len(plans) == 1
    assert plans[0].exact_ids == frozenset({1, 2})


def test_clearly_different_items_stay_apart():
    plans = build_plans(
        [
            _story(1, APPLE, "The phone adds a brighter sensor.", content_hash="a"),
            _story(2, PYTHON, "The release notes describe the interpreter.", content_hash="b"),
        ]
    )
    assert plans == []


def test_same_event_different_text_is_one_cluster():
    plans = build_plans(
        [
            _story(
                1,
                ARTEMIS_A,
                "The crew touched down near the south pole and began surface checks.",
                content_hash="a" * 64,
            ),
            _story(
                2,
                ARTEMIS_B,
                "NASA said the landing ended the outbound flight and surface work starts now.",
                content_hash="b" * 64,
            ),
        ]
    )
    assert len(plans) == 1
    assert plans[0].member_ids == (1, 2)
    assert plans[0].exact_ids == frozenset()


def test_brand_lookalikes_stay_apart():
    assert not texts_match(
        "Apple выпустила обновление камеры iPhone",
        "Прошивка улучшает ночную съёмку и портреты на последних iPhone.",
        "Samsung выпустила обновление камеры Galaxy",
        "Обновление добавляет режимы ночной съёмки для линейки Galaxy.",
    )
    plans = build_plans(
        [
            _story(
                1,
                "Apple выпустила обновление камеры iPhone",
                "Прошивка улучшает ночную съёмку и портреты на последних iPhone.",
                content_hash="a",
            ),
            _story(
                2,
                "Samsung выпустила обновление камеры Galaxy",
                "Обновление добавляет режимы ночной съёмки для линейки Galaxy.",
                content_hash="b",
            ),
        ]
    )
    assert plans == []


def test_copied_body_clusters_and_short_footer_does_not():
    article = (
        "The Artemis crew landed near the lunar south pole after a four day flight from Earth. "
        "NASA confirmed the touchdown and said the astronauts will collect samples during the "
        "surface stay. Mission control reported stable life support and a clear link."
    )
    plans = build_plans(
        [
            _story(1, "NASA lands Artemis crew on the Moon", article, content_hash="a"),
            _story(2, "Watch this historic moment", article, content_hash="b"),
        ]
    )
    assert len(plans) == 1
    assert plans[0].exact_ids == frozenset()

    footer = "Subscribe to the channel for more developer videos. Like and hit the bell."
    apart = build_plans(
        [
            _story(3, "Building a voice mirror with Gemini", footer, content_hash="c"),
            _story(4, "How we shipped a new billing pipeline", footer, content_hash="d"),
        ]
    )
    assert apart == []


def test_exact_group_does_not_bridge_an_unrelated_third_item():
    plans = build_plans(
        [
            _story(1, "Pasta water and salt", "boil then drain", content_hash="h"),
            _story(2, ARTEMIS_A, "Touchdown near the south pole.", content_hash="h"),
            _story(3, ARTEMIS_B, "Surface work starts after the landing.", content_hash="c"),
        ]
    )
    assert len(plans) == 1
    assert plans[0].member_ids == (1, 2)
    assert plans[0].exact_ids == frozenset({1, 2})


def test_old_similar_event_stays_separate_but_exact_copy_can_cluster():
    early = datetime(2026, 1, 1)
    late = datetime(2026, 3, 1)
    title = "Python releases a new stable version"
    body_a = "The new version adds security fixes and performance improvements."
    body_b = "This stable version brings performance improvements and security fixes."
    assert build_plans([
        _story(1, title, body_a, published_at=early),
        _story(2, title, body_b, published_at=late),
    ]) == []
    exact = build_plans([
        _story(1, title, body_a, content_hash="same", published_at=early),
        _story(2, title, body_a, content_hash="same", published_at=late),
    ])
    assert len(exact) == 1
    assert exact[0].member_ids == (1, 2)


def test_lexical_chain_does_not_merge_unrelated_endpoints():
    plans = build_plans([
        _story(1, "Alpha rocket launch", "Alpha rocket lifted off successfully"),
        _story(2, "Alpha rocket launch and beta mission", "Alpha rocket and beta mission update"),
        _story(3, "Beta mission update", "Beta mission achieved orbit"),
    ])
    assert all(plan.member_ids != (1, 2, 3) for plan in plans)


def test_empty_feed_and_single_item_have_no_cluster():
    assert build_plans([]) == []
    alone = [_story(7, APPLE, "sensor")]
    assert build_plans(alone) == []
    assert unclustered_count([], alone) == 1
    assert unclustered_count([], []) == 0


def test_empty_publications_do_not_form_an_exact_cluster():
    assert build_plans([
        _story(1, None, "", content_hash="same"),
        _story(2, None, "", content_hash="same"),
    ]) == []


def test_primary_is_longest_body_then_newest_published_at():
    plans = build_plans(
        [
            _story(1, "Same title", "short", content_hash="h", published_at=datetime(2026, 10, 2)),
            _story(
                2, "Other title", "x" * 40, content_hash="h", published_at=datetime(2026, 10, 1)
            ),
        ]
    )
    assert plans[0].primary_id == 2

    tied = build_plans(
        [
            _story(
                1, "Same title", "body-text", content_hash="h", published_at=datetime(2026, 10, 1)
            ),
            _story(
                2, "Same title", "body-text", content_hash="h", published_at=datetime(2026, 10, 3)
            ),
        ]
    )
    assert tied[0].primary_id == 2

    missing_date = build_plans(
        [
            _story(1, "Same title", "body-text", content_hash="h"),
            _story(
                2, "Same title", "body-text", content_hash="h", published_at=datetime(2020, 1, 1)
            ),
        ]
    )
    assert missing_date[0].primary_id == 2

    no_dates = build_plans(
        [
            _story(5, "Same title", "body-text", content_hash="h"),
            _story(4, "Same title", "body-text", content_hash="h"),
        ]
    )
    assert no_dates[0].primary_id == 4


def test_rerun_reuses_cluster_ids_and_summary():
    items = [
        _story(1, ARTEMIS_A, "Touchdown near the south pole.", content_hash="a"),
        _story(2, ARTEMIS_A, "Touchdown near the south pole.", content_hash="a"),
        _story(3, PYTHON, "Interpreter changes.", content_hash="b"),
    ]
    plans = build_plans(items)
    assert len(plans) == 1
    first = assign_clusters([], plans)
    assert first[0].cluster_id is None
    stored = [
        StoredCluster(
            id=4,
            title=first[0].plan.title,
            summary="keep-me",
            members=tuple(
                (feed_id, feed_id == first[0].plan.primary_id)
                for feed_id in first[0].plan.member_ids
            ),
        )
    ]
    second = assign_clusters(stored, build_plans(items))
    assert [slot.cluster_id for slot in second] == [4]
    assert second[0].summary == "keep-me"
    assert second[0].plan.member_ids == first[0].plan.member_ids

    extra = _story(9, ARTEMIS_A, "Touchdown near the south pole.", content_hash="a")
    grown = assign_clusters(stored, build_plans([*items, extra]))
    assert len(grown) == 1
    assert grown[0].cluster_id == 4
    assert 9 in grown[0].plan.member_ids
    assert stale_cluster_ids(stored, grown) == []


def test_empty_rerun_drops_stale_clusters_only():
    stored = [StoredCluster(id=3, title="old", summary=None, members=((1, True), (2, False)))]
    assigned = assign_clusters(stored, [])
    assert assigned == []
    assert stale_cluster_ids(stored, assigned) == [3]


def test_cli_fixture_prints_primary_exact_and_leaves_different_item(capsys):
    assert main(["--fixture", str(FIXTURE)]) == 0
    output = capsys.readouterr().out
    assert "cluster=new-1\tprimary=3\t" in output
    assert "\n3\tprimary\tsimilar\t" in output
    assert "\n1\tmember\texact\t" in output
    assert "\n2\tmember\texact\t" in output
    assert "Python" not in output
    assert "clusters=1 unclustered=1" in output


def test_cli_dry_run_fixture_marks_dry_run(capsys):
    assert main(["--fixture", str(FIXTURE), "--dry-run"]) == 0
    assert "dry-run" in capsys.readouterr().out


def test_cli_missing_fixture_exits_2():
    assert main(["--fixture", "tests/fixtures/cluster/missing.json"]) == 2


def test_report_uses_database_id_when_present():
    items = [
        _story(1, "Same headline", "same body"),
        _story(2, "Same headline", "same body"),
    ]
    plans = build_plans(items)
    views = cluster_views(plans, items, [15])
    text = format_report(views, unclustered=0, dry_run=False)
    assert text.startswith("cluster=15\tprimary=")
    assert "exact" in text
    assert "clusters=1 unclustered=0" in text


def test_primary_is_demoted_before_another_member_is_promoted():
    links = [
        ClusterItem(cluster_id=4, feed_item_id=1, is_primary=True),
        ClusterItem(cluster_id=4, feed_item_id=2, is_primary=False),
    ]

    class FakeSession:
        def scalars(self, _statement):
            class Rows:
                def all(self):
                    return links
            return Rows()

        def flush(self):
            assert sum(link.is_primary for link in links) == 0

        def delete(self, _link):
            raise AssertionError("No member should be deleted")

        def add(self, _link):
            raise AssertionError("No member should be added")

    plan = build_plans([
        _story(1, "Same headline", "short", content_hash="same"),
        _story(2, "Same headline", "much longer body", content_hash="same"),
    ])[0]
    _sync_members(FakeSession(), 4, plan)
    assert [(link.feed_item_id, link.is_primary) for link in links] == [(1, False), (2, True)]
