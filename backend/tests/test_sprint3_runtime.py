"""Real Beat -> Redis -> Worker -> PostgreSQL -> authenticated FastAPI.

Only Habr HTTP and paid AI providers are faked. Requires disposable services.
"""

import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from celery.contrib.testing.worker import start_worker
from celery.signals import task_postrun
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.ai.dto import RelevanceResult
from app.connectors.habr.client import HabrClient
from app.core.config import get_settings
from app.db.session import get_engine
from app.main import app
from app.models import ContentCluster, FeedItem, UserInterest, UserItemScore
from app.models.enums import ProcessingStatus
from tests.test_feedback_postgres import _test_database
from tests.test_pipeline_analysis import _Importance, _Summarizer

pytestmark = pytest.mark.skipif(
    not (os.environ.get("ITD89_TEST_DATABASE_URL") and os.environ.get("SPRINT3_TEST_REDIS_URL")),
    reason="Requires disposable PostgreSQL and Redis",
)


def _wait(predicate, seconds=25):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if predicate():
            return
        time.sleep(0.1)
    raise AssertionError("Timed out waiting for the real worker")


@pytest.mark.parametrize("provider", ["legacy", "openai"])
def test_scheduled_pipeline_to_ranked_feed_and_frozen_digest(monkeypatch, tmp_path, provider):
    from app.pipeline import analysis
    from app.services import user_relevance
    from app.workers.celery_app import celery_app

    with _test_database("head") as database:
        monkeypatch.setenv("DATABASE_URL", database.url)
        monkeypatch.setenv("REDIS_URL", os.environ["SPRINT3_TEST_REDIS_URL"])
        monkeypatch.setenv("SCHEDULED_PIPELINE_ENABLED", "true")
        monkeypatch.setenv("SCHEDULED_PIPELINE_INTERVAL_SECONDS", "2")
        monkeypatch.setenv("SCHEDULED_PIPELINE_SOURCE_LIMIT", "3")
        monkeypatch.setenv("SCHEDULED_PAID_AI_ENABLED", "true")
        get_settings.cache_clear()
        get_engine.cache_clear()
        old_broker = celery_app.conf.broker_url
        celery_app.conf.broker_url = os.environ["SPRINT3_TEST_REDIS_URL"]
        monkeypatch.setenv("LLM_PROVIDER", "openai" if provider == "openai" else "openrouter")
        monkeypatch.setenv("IMPORTANCE_PROVIDER", "openai" if provider == "openai" else "typesafe")
        monkeypatch.setenv("RELEVANCE_PROVIDER", "openai" if provider == "openai" else "typesafe")
        get_settings.cache_clear()
        summary, importance = _Summarizer(), _Importance()
        if provider == "legacy":
            monkeypatch.setattr(analysis, "_default_summarization_service", lambda: summary)
            monkeypatch.setattr(analysis, "_default_importance_service", lambda: importance)

        class Scoring:
            def __init__(self, *args):
                pass

            def validate_configuration(self):
                pass

            async def score_relevance(self, value, **kwargs):
                is_cluster = "cluster" in value["summary"]
                likes_cluster = "space" in value["interests"]
                score = 0.95 if is_cluster == likes_cluster else 0.05
                return SimpleNamespace(
                    reused=False,
                    result=RelevanceResult(
                        relevance=score, fingerprint=f"v1:{'c' * 64}", provider="test", model="test"
                    ),
                )

        if provider == "legacy":
            monkeypatch.setattr(user_relevance, "RelevanceService", Scoring)
            monkeypatch.setattr(user_relevance, "create_relevance_adapter", lambda _: object())
        else:
            # Real factories, services, fingerprints, persistence and Celery dispatch;
            # replace only OpenAI HTTP, never the provider-selection logic.
            from tests.test_ai_openai import payload

            monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
            monkeypatch.setenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
            monkeypatch.setenv("OPENAI_MODEL", "gpt-5.4-mini")
            get_settings.cache_clear()
            real_client = httpx.AsyncClient

            def respond(request):
                assert str(request.url) == "https://api.openai.com/v1/responses"
                assert request.headers["Authorization"] == "Bearer test-openai-key"
                body = json.loads(request.content)
                assert body["store"] is False
                schema = body.get("text", {}).get("format", {}).get("name")
                if schema == "importance_scores":
                    importance.calls.append("importance")
                    answer = json.dumps(
                        {"scale_score": 2, "significance_score": 2, "reaction_score": 1}
                    )
                elif schema == "relevance_score":
                    value = json.loads(body["input"][1]["content"])
                    matched = ("cluster" in value["summary"]) == ("space" in value["interests"])
                    answer = json.dumps({"relevance": 0.95 if matched else 0.05})
                else:
                    raw = body["input"][1]["content"]
                    value = json.loads(raw[raw.index("{") :])
                    summary.calls.append(value["kind"])
                    answer = f"Выжимка {value['kind']} {value['id']}"
                return httpx.Response(200, json=payload(answer))

            monkeypatch.setattr(
                httpx,
                "AsyncClient",
                lambda **kwargs: real_client(transport=httpx.MockTransport(respond), **kwargs),
            )
        fetches, details, completed = [], [], []
        titles = [
            "NASA landed the Artemis crew on the Moon after a four-day flight",
            "Artemis crew has landed on the Moon, NASA confirms the four-day mission",
            "Apple unveiled the iPhone 17 with a new camera",
            "This fourth article must never be downloaded",
        ]

        async def page(self, **kwargs):
            fetches.append(1)
            return {
                "articleIds": [str(i) for i in range(1, 5)],
                "articleRefs": {
                    str(i): {
                        "id": str(i),
                        "titleHtml": title,
                        "timePublished": datetime.now(UTC).isoformat(),
                    }
                    for i, title in enumerate(titles, 1)
                },
            }

        async def detail(self, article_id):
            details.append(article_id)
            return titles[int(article_id) - 1]

        monkeypatch.setattr(HabrClient, "get_articles_page", page)
        monkeypatch.setattr(HabrClient, "get_full_article_content", detail)

        def finished(sender=None, retval=None, **kwargs):
            if sender.name == "pipeline.run_scheduled":
                completed.append(retval)

        task_postrun.connect(finished, weak=False)
        beat = None
        try:
            with TestClient(app) as client:
                headers, user_ids = [], []
                for i, interest in enumerate(["space", "phones"], 1):
                    credentials = {"email": f"sprint{i}@example.test", "password": "test-password"}
                    created = client.post("/api/auth/register", json=credentials)
                    assert created.status_code == 201, created.text
                    uid = created.json()["id"]
                    user_ids.append(uid)
                    login = client.post("/api/auth/login", json=credentials)
                    assert login.status_code == 200, login.text
                    header = {"Authorization": f"Bearer {login.json()['token']}"}
                    headers.append(header)
                    attached = client.post(
                        f"/api/users/{uid}/sources",
                        headers=header,
                        json={"type": "habr", "url": HabrClient.RSS_URL},
                    )
                    assert attached.status_code in (200, 201), attached.text
                    with Session(database.engine) as db, db.begin():
                        db.add(UserInterest(user_id=uid, text=interest))

                with start_worker(
                    celery_app,
                    pool="solo",
                    perform_ping_check=False,
                    loglevel="INFO",
                    shutdown_timeout=10,
                ):
                    with (tmp_path / "beat.log").open("w") as output:
                        beat = subprocess.Popen(
                            [
                                sys.executable,
                                "-m",
                                "celery",
                                "-A",
                                "app.workers.celery_app:celery_app",
                                "beat",
                                "--loglevel=INFO",
                                f"--schedule={tmp_path / 'beat-schedule'}",
                            ],
                            cwd=Path(__file__).resolve().parents[1],
                            env=os.environ.copy(),
                            stdout=output,
                            stderr=subprocess.STDOUT,
                        )
                        _wait(lambda: bool(completed))
                        beat.terminate()
                        beat.wait(timeout=10)
                    assert completed[0]["status"] == "completed", completed
                    assert len(fetches) == 1  # two subscribers, one shared fetch
                    assert details == ["1", "2", "3"]

                    def scored():
                        with Session(database.engine) as db:
                            return db.scalar(select(func.count()).select_from(UserItemScore)) == 4

                    _wait(scored)
                    with Session(database.engine) as db:
                        assert db.scalar(select(func.count()).select_from(FeedItem)) == 3
                        assert db.scalar(select(func.count()).select_from(ContentCluster)) == 1
                        assert set(db.scalars(select(FeedItem.processing_status))) == {
                            ProcessingStatus.PROCESSED
                        }
                    assert len(summary.calls) == 2  # cluster + singleton, not 3 articles
                    for uid, header, cluster_first in zip(
                        user_ids, headers, [True, False], strict=True
                    ):
                        feed = client.get(f"/api/feed?user_id={uid}", headers=header)
                        assert feed.status_code == 200, feed.text
                        cards = feed.json()["items"]
                        assert len(cards) == 2
                        assert cards[0]["is_cluster"] is cluster_first
                        digest = client.post(
                            "/api/digests/generate", headers=header, json={"user_id": uid}
                        )
                        assert digest.status_code == 201, digest.text
                        assert digest.json()["items"][0]["is_cluster"] is cluster_first
                        cluster = next(card for card in cards if card["is_cluster"])
                        favorite = client.post(
                            "/api/favorites",
                            headers=header,
                            json={"cluster_id": cluster["cluster_id"]},
                        )
                        assert favorite.status_code == 201, favorite.text
                        feedback = client.put(
                            f"/api/users/{uid}/feedback/cluster/{cluster['cluster_id']}",
                            headers=header,
                            json={"reaction": "interested"},
                        )
                        assert feedback.status_code == 201, feedback.text
                    # Same collected data and unchanged analysis inputs reuse AI and storage.
                    # Restart the real Beat: a manual delay() does not establish that
                    # the next scheduled tick is idempotent or survives a Beat restart.
                    with (tmp_path / "beat-restarted.log").open("w") as output:
                        beat = subprocess.Popen(
                            [
                                sys.executable,
                                "-m",
                                "celery",
                                "-A",
                                "app.workers.celery_app:celery_app",
                                "beat",
                                "--loglevel=INFO",
                                f"--schedule={tmp_path / 'beat-schedule'}",
                            ],
                            cwd=Path(__file__).resolve().parents[1],
                            env=os.environ.copy(),
                            stdout=output,
                            stderr=subprocess.STDOUT,
                        )
                        _wait(lambda: len(completed) >= 2)
                        beat.terminate()
                        beat.wait(timeout=10)
                    assert completed[1]["status"] == "completed"
                    assert completed[1]["sources"]["total"] == 1
                    assert completed[1]["sources"]["inserted"] == 0
                    assert completed[1]["ai"]["selected"] == 0
                    assert completed[1]["ai"]["queue_succeeded"] == 0
                    assert len(details) == 3
                    assert len(summary.calls) == 2
                    assert len(importance.calls) == 2
                    original = digest.json()
                    with Session(database.engine) as db, db.begin():
                        db.get(ContentCluster, cluster["cluster_id"]).title = "Changed cluster"
                    saved = client.get(
                        f"/api/digests/{original['id']}?user_id={user_ids[-1]}", headers=headers[-1]
                    )
                    assert saved.json() == original
        finally:
            if beat is not None and beat.poll() is None:
                beat.terminate()
                beat.wait(timeout=10)
            task_postrun.disconnect(finished)
            celery_app.conf.broker_url = old_broker
            if get_engine.cache_info().currsize:
                get_engine().dispose()
            get_engine.cache_clear()
            get_settings.cache_clear()
