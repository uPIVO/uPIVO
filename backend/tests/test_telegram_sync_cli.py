import asyncio

from app.connectors.telegram.models import ResolvedTelegramChannel
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

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return None

    async def resolve_channel(self, source):
        return self.channel

    async def fetch_latest_posts(self, channel, *, limit):
        self.latest_limits.append(limit)
        return []

    async def fetch_new_posts(self, channel, *, after_message_id):
        self.cursors.append(after_message_id)
        return []


def test_runner_forwards_external_cursor_and_fetches_all_new_posts(monkeypatch):
    fake = FakeConnector()
    monkeypatch.setattr(telegram_sync, "TelegramConnector", lambda: fake)

    title, channel_id, posts = asyncio.run(
        telegram_sync.collect("@testchannel", after_message_id=123, limit=7)
    )

    assert (title, channel_id, posts) == ("Test channel", 42, [])
    assert fake.cursors == [123]
    assert fake.latest_limits == []


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
