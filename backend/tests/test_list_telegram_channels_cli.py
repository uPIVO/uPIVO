import json

import pytest

from app.connectors.telegram.errors import TelegramConnectorError
from app.connectors.telegram.models import TelegramChannelInfo
from scripts import list_telegram_channels


class FakeConnector:
    def __init__(self, channels, error=None):
        self.channels = channels
        self.error = error
        self.entered = False
        self.exited = False
        self.listed = False

    async def __aenter__(self):
        self.entered = True
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        self.exited = True

    async def list_broadcast_channels(self):
        self.listed = True
        if self.error is not None:
            raise self.error
        return self.channels


def test_cli_uses_connector_context_and_safely_prints_channel_title(monkeypatch, capsys):
    title = 'Канал\t"Новости"'
    connector = FakeConnector([TelegramChannelInfo(marked_id=-1001234567890, title=title)])
    monkeypatch.setattr(list_telegram_channels, "TelegramConnector", lambda: connector)

    exit_code = list_telegram_channels.main([])

    assert exit_code == 0
    assert capsys.readouterr().out == (
        "Каналов: 1\n"
        "ID канала\tНазвание\n"
        f"-1001234567890\t{json.dumps(title, ensure_ascii=False)}\n"
    )
    assert connector.entered
    assert connector.exited
    assert connector.listed


def test_cli_reports_no_channels_when_list_is_empty(monkeypatch, capsys):
    connector = FakeConnector([])
    monkeypatch.setattr(list_telegram_channels, "TelegramConnector", lambda: connector)

    exit_code = list_telegram_channels.main([])

    assert exit_code == 0
    assert capsys.readouterr().out == "Каналы не найдены.\n"
    assert connector.entered
    assert connector.exited


def test_cli_returns_one_for_connector_error(monkeypatch, capsys):
    error_message = "Не удалось получить список каналов Telegram."
    connector = FakeConnector([], error=TelegramConnectorError(error_message))
    monkeypatch.setattr(list_telegram_channels, "TelegramConnector", lambda: connector)

    exit_code = list_telegram_channels.main([])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert captured.out == ""
    assert captured.err == f"{error_message}\n"
    assert connector.entered
    assert connector.exited


def test_help_uses_russian_wording(capsys):
    with pytest.raises(SystemExit) as caught:
        list_telegram_channels.main(["--help"])

    assert caught.value.code == 0
    output = capsys.readouterr().out
    assert "Показать названия и полные ID каналов Telegram-аккаунта." in output
    assert "Параметры" in output
    assert "Показать эту справку и завершить работу." in output
    assert "options" not in output
