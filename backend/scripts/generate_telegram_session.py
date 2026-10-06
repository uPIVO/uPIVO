import asyncio
import getpass
import sys
from collections.abc import Callable
from pathlib import Path

from app.connectors.telegram.errors import TelegramConfigurationError
from app.connectors.telegram.session import save_string_session_to_env


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
            "Create the root .env from .env.example before generating a session."
        )

    try:
        settings = get_settings()
    except Exception:
        raise TelegramConfigurationError(
            "Could not read Telegram settings from the local .env."
        ) from None
    api_id_value = settings.telegram_api_id
    api_hash = settings.telegram_api_hash.get_secret_value()
    if not api_id_value or not api_hash:
        raise TelegramConfigurationError(
            "Set TELEGRAM_API_ID and TELEGRAM_API_HASH in the root .env first."
        )
    try:
        api_id = int(api_id_value)
    except (TypeError, ValueError):
        raise TelegramConfigurationError("TELEGRAM_API_ID must be an integer.") from None

    try:
        from telethon import TelegramClient
        from telethon.errors import SessionPasswordNeededError
        from telethon.sessions import StringSession
    except ImportError:
        raise TelegramConfigurationError(
            "Telethon is unavailable; install the locked backend dependencies."
        ) from None

    try:
        session = StringSession()
        client = TelegramClient(session, api_id, api_hash)
    except Exception:
        raise TelegramConfigurationError(
            "Could not initialize the Telegram client; check local configuration."
        ) from None
    try:
        await client.connect()
        phone = phone_prompt("Telegram phone number (international format): ").strip()
        if not phone:
            raise TelegramConfigurationError("A phone number is required.")
        sent_code = await client.send_code_request(phone)
        code = secret_prompt("Telegram login code: ").strip()
        try:
            await client.sign_in(phone=phone, code=code, phone_code_hash=sent_code.phone_code_hash)
        except SessionPasswordNeededError:
            password = secret_prompt("Telegram 2FA password: ")
            await client.sign_in(password=password)

        string_session = StringSession.save(client.session)
        if not string_session:
            raise TelegramConfigurationError("Telegram did not return an authorized session.")
        save_string_session_to_env(path, string_session)
    except TelegramConfigurationError:
        raise
    except Exception:
        raise TelegramConfigurationError(
            "Telegram login failed; verify the code/account and retry. "
            "The session was not displayed."
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
    print("Telegram StringSession saved to the ignored root .env; its value was not displayed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
