import asyncio

from telethon.errors import SessionPasswordNeededError

from scripts import generate_telegram_session


class FakeQrLogin:
    url = "tg://login?token=example"

    def __init__(self, *, expire_once=False, password_required=False):
        self.expire_once = expire_once
        self.password_required = password_required
        self.waits = 0
        self.recreates = 0

    async def wait(self):
        self.waits += 1
        if self.expire_once and self.waits == 1:
            raise TimeoutError
        if self.password_required:
            raise SessionPasswordNeededError(request=None)

    async def recreate(self):
        self.recreates += 1
        self.url = "tg://login?token=refreshed"


class FakeClient:
    def __init__(self, login):
        self.login = login
        self.password = None

    async def qr_login(self):
        return self.login

    async def sign_in(self, *, password):
        self.password = password


def test_qr_login_refreshes_expired_token(capsys):
    login = FakeQrLogin(expire_once=True)
    client = FakeClient(login)
    asyncio.run(generate_telegram_session._login_via_qr(client, lambda _: "unused"))

    assert login.waits == 2
    assert login.recreates == 1
    assert "QR истёк" in capsys.readouterr().out


def test_qr_login_asks_for_two_factor_password():
    login = FakeQrLogin(password_required=True)
    client = FakeClient(login)
    asyncio.run(generate_telegram_session._login_via_qr(client, lambda _: "local-password"))

    assert client.password == "local-password"


def test_qr_option_selects_qr_flow(monkeypatch):
    selected = []

    async def fake_generate_local_session(*, qr):
        selected.append(qr)

    monkeypatch.setattr(
        generate_telegram_session, "generate_local_session", fake_generate_local_session
    )
    assert generate_telegram_session.main(["--qr"]) == 0
    assert selected == [True]
