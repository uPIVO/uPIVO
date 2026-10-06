import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.connectors.telegram.client import TelegramConnector
from app.connectors.telegram.errors import TelegramAuthorizationError, TelegramChannelAccessError


class FakeTelegramClient:
    def __init__(self, messages=(), *, authorized=True, entity=None, entity_error=None, dialogs=()):
        self.messages = list(messages)
        self.authorized = authorized
        self.entity = entity or SimpleNamespace(
            id=42, title="Test channel", username="testchannel", broadcast=True
        )
        self.entity_error = entity_error
        self.dialogs = list(dialogs)
        self.connected = False
        self.resolved_sources = []
        self.iter_calls = []

    def is_connected(self):
        return self.connected

    async def connect(self):
        self.connected = True

    async def disconnect(self):
        self.connected = False

    async def is_user_authorized(self):
        return self.authorized

    async def get_entity(self, source):
        self.resolved_sources.append(source)
        if self.entity_error:
            raise self.entity_error
        return self.entity

    def iter_dialogs(self):
        async def iterate():
            for dialog in self.dialogs:
                yield dialog

        return iterate()

    def iter_messages(self, entity, **kwargs):
        self.iter_calls.append((entity, kwargs))

        async def iterate():
            min_id = kwargs.get("min_id", 0)
            max_id = kwargs.get("max_id")
            for item in self.messages:
                if item.id > min_id and (max_id is None or item.id < max_id):
                    yield item

        return iterate()


def message(message_id, text="", *, grouped_id=None, media=None, forward=False, action=None):
    return SimpleNamespace(
        id=message_id,
        date=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=message_id),
        message=text,
        grouped_id=grouped_id,
        media=media,
        fwd_from=object() if forward else None,
        action=action,
    )


def test_latest_limit_counts_logical_posts_and_keeps_complete_album_and_textless_post():
    api = FakeTelegramClient(
        [
            message(110, "Newest"),
            message(109, "Second caption", grouped_id=7, forward=True),
            message(108, "First caption", grouped_id=7),
            message(107, media=object()),
            message(106, action=object()),
            message(105, "Older"),
        ]
    )

    async def fetch():
        async with TelegramConnector(client=api) as connector:
            return await connector.fetch_latest_posts("@testchannel", limit=3)

    posts = asyncio.run(fetch())

    assert [post.message_id for post in posts] == [107, 109, 110]
    assert [post.external_id for post in posts] == ["message:107", "album:7", "message:110"]
    assert posts[0].text == ""
    assert posts[0].message_ids == (107,)
    assert posts[1].text == "First caption\nSecond caption"
    assert posts[1].message_ids == (108, 109)
    assert posts[1].grouped_id == 7
    assert posts[1].is_forward is True
    assert posts[1].published_at == message(108).date
    assert api.resolved_sources == ["testchannel"]
    assert api.iter_calls[0][1] == {}


def test_new_fetch_returns_all_after_external_cursor_in_chronological_order():
    api = FakeTelegramClient(
        [
            message(205, "newest"),
            message(203, "album caption", grouped_id=8),
            message(202, media=object(), grouped_id=8),
            message(201, "first new"),
            message(200, "cursor itself"),
        ]
    )

    async def fetch():
        async with TelegramConnector(client=api) as connector:
            return await connector.fetch_new_posts(
                "https://t.me/s/testchannel", after_message_id=200
            )

    posts = asyncio.run(fetch())

    assert [post.message_id for post in posts] == [201, 203, 205]
    assert [post.external_id for post in posts] == ["message:201", "album:8", "message:205"]
    assert posts[1].message_ids == (202, 203)
    assert api.resolved_sources == ["testchannel"]
    assert api.iter_calls[0][1] == {"min_id": 200}


def test_incremental_fetch_reconstructs_album_bisected_by_external_cursor():
    api = FakeTelegramClient(
        [
            message(205, "newest"),
            message(203, "last caption", grouped_id=9),
            message(202, "first caption", grouped_id=9),
            message(201, "previous post"),
        ]
    )

    async def fetch():
        async with TelegramConnector(client=api) as connector:
            return await connector.fetch_new_posts("@testchannel", after_message_id=202)

    posts = asyncio.run(fetch())

    assert [post.external_id for post in posts] == ["album:9", "message:205"]
    assert posts[0].message_id == 203
    assert posts[0].message_ids == (202, 203)
    assert posts[0].text == "first caption\nlast caption"
    assert [call[1] for call in api.iter_calls] == [
        {"min_id": 202},
        {"max_id": 203},
    ]


def test_private_channel_access_error_and_logs_do_not_include_raw_exception_or_input(caplog):
    secret_input = "@private_channel_token"
    api = FakeTelegramClient(entity_error=ValueError("secret session detail"))

    async def resolve():
        async with TelegramConnector(client=api) as connector:
            with pytest.raises(TelegramChannelAccessError, match="already a member") as caught:
                await connector.resolve_channel(secret_input)
        return caught

    caught = asyncio.run(resolve())

    assert "secret session detail" not in str(caught.value)
    assert "Telegram channel access failed" in caplog.text
    assert "error_type=ValueError" in caplog.text
    assert secret_input not in caplog.text
    assert "secret session detail" not in caplog.text
    assert api.resolved_sources == ["private_channel_token"]


def test_unauthorized_session_fails_without_starting_interactive_login():
    api = FakeTelegramClient(authorized=False)

    async def connect():
        with pytest.raises(TelegramAuthorizationError, match="session generator"):
            async with TelegramConnector(client=api):
                pytest.fail("unauthorized Telegram session must not enter the connector")

    asyncio.run(connect())

    assert api.connected is False
    assert not hasattr(api, "start")


def test_invite_links_are_rejected_without_attempting_to_join():
    api = FakeTelegramClient()

    async def resolve():
        async with TelegramConnector(client=api) as connector:
            with pytest.raises(TelegramChannelAccessError, match="Invite links are not accepted"):
                await connector.resolve_channel("https://t.me/+invite_secret")

    asyncio.run(resolve())

    assert api.resolved_sources == []


def test_signed_private_channel_id_resolves_from_existing_dialogs_without_joining():
    entity = SimpleNamespace(id=123456789, title="Private channel", username=None, megagroup=True)
    api = FakeTelegramClient(
        entity_error=ValueError("channel entity missing from cache"),
        dialogs=[SimpleNamespace(entity=entity)],
    )

    async def resolve():
        async with TelegramConnector(client=api) as connector:
            return await connector.resolve_channel("-1000123456789")

    channel = asyncio.run(resolve())

    assert channel.channel_id == 123456789
    assert api.resolved_sources == [-1000123456789]
    assert not hasattr(api, "join_channel")


def test_source_aliases_resolve_to_canonical_identifier_forms():
    from app.connectors.telegram.client import normalize_channel_source

    assert normalize_channel_source(" @example ") == "example"
    assert normalize_channel_source("https://t.me/s/example") == "example"
    assert normalize_channel_source("-1000123456789") == -1000123456789
    assert normalize_channel_source("https://t.me/c/123456789") == -1000123456789
    with pytest.raises(TelegramChannelAccessError, match="full Telethon marked channel ID"):
        normalize_channel_source("-100123456789")
