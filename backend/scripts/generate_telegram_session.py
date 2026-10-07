import asyncio
import getpass
import sys
from collections.abc import Callable
from pathlib import Path

from app.connectors.telegram.credentials import TelegramApiCredentials
from app.connectors.telegram.errors import TelegramConfigurationError
from app.connectors.telegram.session import save_string_session_to_env


def _code_delivery_hint(sent_code: object) -> str:
    delivery_type = type(getattr(sent_code, "type", None)).__name__
    if delivery_type == "SentCodeTypeApp":
        return "Код отправлен в проверенный чат Telegram на активном устройстве."
    if delivery_type == "SentCodeTypeSms":
        return "Код отправлен по SMS."
    if delivery_type == "SentCodeTypeEmailCode":
        return "Код отправлен на почту, привязанную к Telegram."
    return "Telegram принял запрос кода; проверьте активные устройства, SMS и почту."


async def generate_local_session(
    *,
    env_path: Path | None = None,
    phone_prompt: Callable[[str], str] = input,
    secret_prompt: Callable[[str], str] = getpass.getpass,
) -> None:
    from app.core.config import ROOT_ENV, get_settings

    path = env_path or ROOT_ENV
    if not path.is_file():
        raise TelegramConfigurationError(
            "Создайте корневой .env из .env.example перед созданием сессии."
        )

    try:
        settings = get_settings()
    except Exception:
        raise TelegramConfigurationError(
            "Не удалось загрузить настройки Telegram из корневого .env."
        ) from None
    credentials = TelegramApiCredentials.from_settings(settings)

    try:
        from telethon import TelegramClient
        from telethon.errors import SessionPasswordNeededError
        from telethon.sessions import StringSession
    except ImportError:
        raise TelegramConfigurationError(
            "Telethon недоступен; установите зависимости backend из lock-файла."
        ) from None

    try:
        session = StringSession()
        client = TelegramClient(
            session,
            credentials.api_id,
            credentials.api_hash.get_secret_value(),
        )
    except Exception:
        raise TelegramConfigurationError(
            "Не удалось создать клиент Telethon; проверьте локальные настройки."
        ) from None
    try:
        await client.connect()
        phone = phone_prompt("Номер телефона Telegram (международный формат): ").strip()
        if not phone:
            raise TelegramConfigurationError("Укажите номер телефона.")
        sent_code = await client.send_code_request(phone)
        print(_code_delivery_hint(sent_code))
        code = secret_prompt("Код входа Telegram: ").strip()
        try:
            await client.sign_in(phone=phone, code=code, phone_code_hash=sent_code.phone_code_hash)
        except SessionPasswordNeededError:
            password = secret_prompt("Пароль двухфакторной аутентификации: ")
            await client.sign_in(password=password)

        string_session = StringSession.save(client.session)
        if not string_session:
            raise TelegramConfigurationError("Telegram не вернул авторизованную сессию.")
        save_string_session_to_env(path, string_session)
    except TelegramConfigurationError:
        raise
    except Exception:
        raise TelegramConfigurationError(
            "Вход в Telegram не выполнен; проверьте код и попробуйте снова. "
            "Значение сессии не выводилось."
        ) from None
    finally:
        try:
            if client.is_connected():
                await client.disconnect()
        except Exception:
            pass


def main() -> int:
    try:
        asyncio.run(generate_local_session())
    except TelegramConfigurationError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print("Сессия StringSession сохранена в корневом .env; её значение не выводилось.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
