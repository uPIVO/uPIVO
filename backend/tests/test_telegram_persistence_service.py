import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql

from app.connectors.telegram.models import ResolvedTelegramChannel, TelegramPost
from app.models.telegram_sync_state import TelegramSyncState
from app.services.telegram import TelegramSyncError, sync_telegram_channel
from scripts import collect_telegram


class _Result:
    def __init__(self, value=None):
        self.value = value

    def scalar_one(self):
        return self.value

    def scalar_one_or_none(self):
        return self.value


class _Session:
    def __init__(self, factory, transactional=False):
        self.factory = factory
        self.transactional = transactional

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if self.transactional:
            self.factory.in_transaction = False
            self.factory.rolled_back = exc_type is not None

    def begin(self):
        self.transactional = True
        self.factory.in_transaction = True
        return self

    def execute(self, statement):
        sql = str(statement.compile(dialect=postgresql.dialect()))
        self.factory.statements.append((statement, sql))
        if "JOIN sources" in sql:
            return _Result(self.factory.start_cursor)
        if "FOR UPDATE" in sql:
            return _Result(self.factory.actual_cursor)
        if "INSERT INTO sources" in sql:
            assert self.factory.in_transaction
            return _Result(81)
        if "INSERT INTO feed_items" in sql:
            assert self.factory.in_transaction
            self.factory.saved_items += 1
            if self.factory.fail_item_number == self.factory.saved_items:
                raise ValueError("failed item")
        return _Result()


class _Factory:
    def __init__(self, start_cursor=None, actual_cursor=0, fail_item_number=None):
        self.start_cursor = start_cursor
        self.actual_cursor = actual_cursor
        self.fail_item_number = fail_item_number
        self.statements = []
        self.in_transaction = False
        self.rolled_back = False
        self.saved_items = 0

    def __call__(self):
        return _Session(self)

    def begin(self):
        session = _Session(self, transactional=True)
        self.in_transaction = True
        return session


class _Connector:
    def __init__(self, posts, factory):
        self.posts = posts
        self.factory = factory
        self.calls = []

    async def fetch_latest_posts(self, channel, *, limit):
        assert not self.factory.in_transaction
        self.calls.append(("latest", limit))
        return self.posts

    async def fetch_new_posts(self, channel, *, after_message_id, limit):
        assert not self.factory.in_transaction
        self.calls.append(("new", after_message_id, limit))
        return self.posts


def _channel():
    return ResolvedTelegramChannel(456, "Канал", SimpleNamespace(id=456, username="channel"))


def _post(message_id=5):
    return TelegramPost(
        external_id=f"message:{message_id}",
        message_id=message_id,
        text="Текст",
        published_at=datetime(2025, 1, 1, tzinfo=UTC),
    )


@pytest.mark.parametrize("limit, expected", [(None, 50), (3, 3)])
def test_initial_fetch_and_save_advance_cursor(limit, expected):
    factory = _Factory()
    connector = _Connector([_post()], factory)

    count, cursor = asyncio.run(
        sync_telegram_channel(_channel(), connector, factory, limit=limit)
    )

    assert connector.calls == [("latest", expected)]
    assert (count, cursor) == (1, 5)
    assert factory.saved_items == 1
    cursor_updates = [
        statement
        for statement, _sql in factory.statements
        if getattr(getattr(statement, "table", None), "name", None) == "telegram_sync_state"
        and statement.is_update
    ]
    assert len(cursor_updates) == 1
    assert cursor_updates[0]._values[TelegramSyncState.__table__.c.last_message_id].value == 5
    source_sql = next(sql for _statement, sql in factory.statements if "INSERT INTO sources" in sql)
    assert "WHERE type = 'telegram'" in source_sql
    item_sql = next(
        sql for _statement, sql in factory.statements if "INSERT INTO feed_items" in sql
    )
    assert "DO UPDATE SET body_text = excluded.body_text" in item_sql
    assert "original_url = excluded.original_url" in item_sql
    assert "published_at = excluded.published_at" in item_sql
    assert "raw_payload = excluded.raw_payload" in item_sql
    assert "processing_status" not in item_sql
    assert "summary" not in item_sql
    assert "importance_score" not in item_sql
    assert not factory.rolled_back


def test_album_cursor_uses_maximum_component_id():
    factory = _Factory()
    album = TelegramPost(
        external_id="album:9",
        message_id=12,
        text="Альбом",
        published_at=datetime(2025, 1, 1, tzinfo=UTC),
        grouped_id=9,
        message_ids=(10, 12),
    )
    count, cursor = asyncio.run(
        sync_telegram_channel(_channel(), _Connector([album], factory), factory)
    )
    cursor_update = next(
        statement
        for statement, _sql in factory.statements
        if getattr(getattr(statement, "table", None), "name", None) == "telegram_sync_state"
        and statement.is_update
    )
    assert (count, cursor) == (1, 12)
    assert cursor_update._values[TelegramSyncState.__table__.c.last_message_id].value == 12


def test_existing_cursor_fetches_capped_new_posts():
    factory = _Factory(start_cursor=10, actual_cursor=10)
    connector = _Connector([_post(13)], factory)

    count, cursor = asyncio.run(
        sync_telegram_channel(_channel(), connector, factory, limit=2)
    )

    assert connector.calls == [("new", 10, 2)]
    assert (count, cursor) == (1, 13)


def test_replay_with_empty_batch_keeps_saved_cursor():
    factory = _Factory(start_cursor=20, actual_cursor=20)
    count, cursor = asyncio.run(
        sync_telegram_channel(_channel(), _Connector([], factory), factory)
    )
    assert count == 0
    assert cursor == 20
    assert factory.saved_items == 0
    assert not any(
        getattr(getattr(statement, "table", None), "name", None) == "telegram_sync_state"
        and statement.is_update
        for statement, _sql in factory.statements
    )


def test_cli_displays_safe_cursor_retry_error(monkeypatch, capsys):
    def fail_with_retry(coroutine):
        coroutine.close()
        raise TelegramSyncError("Повторите запуск; публикации не сохранены.")

    monkeypatch.setattr(collect_telegram.asyncio, "run", fail_with_retry)
    assert collect_telegram.main(["@channel"]) == 1
    captured = capsys.readouterr()
    assert "Повторите запуск" in captured.err
    assert "технические подробности скрыты" not in captured.err


def test_race_fails_without_saving_and_database_errors_roll_back_batch():
    race_factory = _Factory(start_cursor=10, actual_cursor=11)
    race_connector = _Connector([_post(12)], race_factory)
    with pytest.raises(TelegramSyncError, match="Курсор канала изменился"):
        asyncio.run(sync_telegram_channel(_channel(), race_connector, race_factory))
    assert race_factory.saved_items == 0
    assert race_factory.rolled_back

    failing_factory = _Factory(fail_item_number=2)
    posts = [_post(5), _post(6)]
    with pytest.raises(ValueError, match="failed item"):
        asyncio.run(
            sync_telegram_channel(
                _channel(), _Connector(posts, failing_factory), failing_factory
            )
        )
    assert failing_factory.saved_items == 2
    assert failing_factory.rolled_back
    assert not any(
        getattr(getattr(statement, "table", None), "name", None) == "telegram_sync_state"
        and statement.is_update
        for statement, _sql in failing_factory.statements
    )
