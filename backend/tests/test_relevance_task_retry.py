"""Do not silently acknowledge failed personal analysis behind a dispatch receipt."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from celery.exceptions import Retry

from app.services.user_relevance import UserRelevanceStatus
from app.workers import tasks


def test_failed_personal_analysis_retries_instead_of_success(monkeypatch):
    class Service:
        async def score_user_content_unit(self, *args):
            return SimpleNamespace(status=UserRelevanceStatus.FAILED)

    monkeypatch.setattr(tasks, "build_default_service", Service)
    retry = Mock(side_effect=Retry())
    monkeypatch.setattr(tasks.score_user_content_unit_task, "retry", retry)
    with pytest.raises(Retry):
        tasks.score_user_content_unit_task(1, "cluster", 2)
    assert retry.call_args.kwargs["countdown"] == 30
    assert tasks.score_user_content_unit_task.max_retries == 3
