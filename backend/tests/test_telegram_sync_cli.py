import asyncio
from datetime import UTC, datetime

import pytest

from app.connectors.telegram.models import ResolvedTelegramChannel, TelegramPost
from scripts import telegram_sync


class FakeConnector:
    def __init__(self):
        self.channel = ResolvedTelegramChannel(
            channel_id=42,
            title="Test channel",
            entity=object(),
        )
        self.latest_limits = []
        self.cursors = []
        self.incremental_limits = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return None

    async def resolve_channel(self, source):
        return self.channel

    async def fetch_latest_posts(self, channel, *, limit):
        self.latest_limits.append(limit)
        return []

    async def fetch_new_posts(self, channel, *, after_message_id, limit=None):
        self.cursors.append(after_message_id)
        self.incremental_limits.append(limit)
        return []


def test_runner_forwards_external_cursor_and_fetches_all_new_posts(monkeypatch):
    fake = FakeConnector()
    monkeypatch.setattr(telegram_sync, "TelegramConnector", lambda: fake)

    title, channel_id, posts = asyncio.run(
        telegram_sync.collect("@testchannel", after_message_id=123, limit=None)
    )

    assert (title, channel_id, posts) == ("Test channel", 42, [])
    assert fake.cursors == [123]
    assert fake.incremental_limits == [None]
    assert fake.latest_limits == []


def test_runner_forwards_explicit_limit_after_cursor(monkeypatch):
    fake = FakeConnector()
    monkeypatch.setattr(telegram_sync, "TelegramConnector", lambda: fake)

    exit_code = telegram_sync.main(["@testchannel", "--after-message-id", "123", "--limit", "7"])

    assert exit_code == 0
    assert fake.cursors == [123]
    assert fake.incremental_limits == [7]
    assert fake.latest_limits == []


def test_runner_keeps_incremental_fetch_unbounded_without_limit(monkeypatch):
    fake = FakeConnector()
    monkeypatch.setattr(telegram_sync, "TelegramConnector", lambda: fake)
    args = telegram_sync.build_parser().parse_args(["@testchannel", "--after-message-id", "123"])

    assert args.limit is None
    assert telegram_sync.main(["@testchannel", "--after-message-id", "123"]) == 0
    assert fake.cursors == [123]
    assert fake.incremental_limits == [None]


def test_runner_uses_default_initial_limit_when_option_is_omitted(monkeypatch):
    fake = FakeConnector()
    monkeypatch.setattr(telegram_sync, "TelegramConnector", lambda: fake)
    args = telegram_sync.build_parser().parse_args(["@testchannel"])

    assert args.limit is None
    title, channel_id, posts = asyncio.run(
        telegram_sync.collect(
            args.source,
            after_message_id=args.after_message_id,
            limit=args.limit,
        )
    )

    assert (title, channel_id, posts) == ("Test channel", 42, [])
    assert fake.latest_limits == [50]
    assert fake.incremental_limits == []


def test_runner_uses_configurable_logical_limit_for_initial_fetch(monkeypatch):
    fake = FakeConnector()
    monkeypatch.setattr(telegram_sync, "TelegramConnector", lambda: fake)
    args = telegram_sync.build_parser().parse_args(["@testchannel", "--limit", "17"])

    title, channel_id, posts = asyncio.run(
        telegram_sync.collect(
            args.source,
            after_message_id=args.after_message_id,
            limit=args.limit,
        )
    )

    assert (title, channel_id, posts) == ("Test channel", 42, [])
    assert fake.latest_limits == [17]
    assert fake.cursors == []


def test_runner_prints_russian_status_and_preserves_machine_flags(capsys):
    post = TelegramPost(
        external_id="album:9",
        message_id=204,
        text="",
        published_at=datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC),
        grouped_id=9,
        is_forward=True,
        message_ids=(203, 204),
    )

    telegram_sync._print_posts("  Тестовый\n канал  ", 42, [post])

    assert capsys.readouterr().out == (
        "Канал: Тестовый канал (ID канала Telegram: 42)\n"
        "Логических публикаций: 1\n"
        "[2026-01-02T03:04:05+00:00 message_id=204 album:9 album=9 forwarded]\n"
        "[публикация без текста]\n\n"
        "Наибольший возвращённый message_id: 204 "
        "(не сохранён; курсором управляет вызывающий код).\n"
    )


@pytest.mark.parametrize(
    ("arguments", "expected_error"),
    [
        (["@testchannel", "--limit", "0"], "--limit должен быть положительным числом"),
        (
            ["@testchannel", "--after-message-id", "-1"],
            "--after-message-id не может быть отрицательным",
        ),
    ],
)
def test_runner_rejects_invalid_options_in_russian(arguments, expected_error, capsys):
    with pytest.raises(SystemExit) as caught:
        telegram_sync.main(arguments)

    assert caught.value.code == 2
    assert expected_error in capsys.readouterr().err


def test_runner_help_uses_russian_group_and_action_labels():
    help_text = telegram_sync.build_parser().format_help()

    assert "Аргументы:" in help_text
    assert "Параметры:" in help_text
    assert "Показать эту справку и завершить работу." in help_text
    assert "options:" not in help_text
    assert "positional arguments:" not in help_text
    assert "show this help message and exit" not in help_text
