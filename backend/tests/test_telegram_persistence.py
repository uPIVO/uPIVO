from datetime import UTC, datetime
from types import SimpleNamespace

from telethon.tl.types import PeerChannel
from telethon.utils import get_peer_id

from app.connectors.telegram.models import ResolvedTelegramChannel, TelegramPost
from app.processing.telegram import map_channel, map_post


def test_maps_public_channel_and_single_post() -> None:
    channel = ResolvedTelegramChannel(
        channel_id=123, title="Канал", entity=SimpleNamespace(username="public_name", id=123)
    )
    post = TelegramPost(
        external_id="message:7",
        message_id=7,
        text="Текст",
        published_at=datetime(2025, 1, 2, 3, tzinfo=UTC),
        message_ids=(7,),
    )

    source = map_channel(channel)
    item = map_post(channel, post)

    assert source["external_id"] == "-1000000000123"
    assert source["canonical_url"] == "https://t.me/public_name"
    assert item["external_id"] == "message:7"
    assert item["body_text"] == "Текст"
    assert item["original_url"] == "https://t.me/public_name/7"
    assert item["published_at"] == datetime(2025, 1, 2, 3)
    assert item["raw_payload"]["published_at"] == "2025-01-02T03:00:00+00:00"


def test_maps_private_album_and_textless_message() -> None:
    channel = ResolvedTelegramChannel(
        channel_id=456, title="Закрытый", entity=SimpleNamespace(username=None, id=456)
    )
    album = TelegramPost(
        external_id="album:99",
        message_id=12,
        text="Фото",
        published_at=datetime(2025, 1, 2, tzinfo=UTC),
        grouped_id=99,
        is_forward=True,
        message_ids=(10, 12),
    )
    empty = TelegramPost(
        external_id="message:13", message_id=13, text="", published_at=datetime(2025, 1, 3)
    )

    source = map_channel(channel)
    mapped_album = map_post(channel, album)
    mapped_empty = map_post(channel, empty)

    assert source["canonical_url"] == "https://t.me/c/456"
    assert source["external_id"] == "-1000000000456"
    assert mapped_album["original_url"] == "https://t.me/c/456/10"
    assert mapped_album["raw_payload"]["message_ids"] == [10, 12]
    assert mapped_album["raw_payload"]["is_forward"] is True
    assert mapped_empty["body_text"] == ""
    assert mapped_empty["raw_payload"]["ai_eligible"] is False
    assert mapped_empty["raw_payload"]["url"] == "https://t.me/c/456/13"


def test_channel_id_uses_telethon_marking_for_long_raw_id() -> None:
    channel = ResolvedTelegramChannel(
        channel_id=123456789, title="", entity=SimpleNamespace(id=123456789)
    )
    marked_id = map_channel(channel)["external_id"]
    assert marked_id == "-1000123456789"
    assert marked_id == str(get_peer_id(PeerChannel(channel.channel_id)))


def test_normalizes_aware_time_to_naive_utc() -> None:
    channel = ResolvedTelegramChannel(channel_id=1, title="", entity=SimpleNamespace(id=1))
    post = TelegramPost(
        external_id="message:1",
        message_id=1,
        text="x",
        published_at=datetime(2025, 1, 1, 5, tzinfo=UTC),
    )
    assert map_post(channel, post)["published_at"] == datetime(2025, 1, 1, 5)
