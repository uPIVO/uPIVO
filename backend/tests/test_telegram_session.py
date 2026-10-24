import stat
import sys

import pytest

from app.connectors.telegram.session import save_string_session_to_env


def test_session_write_preserves_other_env_values_and_restricts_permissions(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text(
        "TELEGRAM_API_ID=12345\n"
        "TELEGRAM_API_HASH=fixture-api-hash\n"
        "TELEGRAM_SESSION=old-fixture-session\n"
        "DATABASE_URL=postgresql://fixture\n",
        encoding="utf-8",
    )
    env_path.chmod(0o644)

    save_string_session_to_env(env_path, "new-fixture-session")

    contents = env_path.read_text(encoding="utf-8")
    assert "TELEGRAM_API_ID=12345\n" in contents
    assert "TELEGRAM_API_HASH=fixture-api-hash\n" in contents
    assert "DATABASE_URL=postgresql://fixture\n" in contents
    assert contents.count("TELEGRAM_SESSION=") == 1
    assert "TELEGRAM_SESSION=new-fixture-session\n" in contents
    if sys.platform != "win32":
        assert stat.S_IMODE(env_path.stat().st_mode) == 0o600


def test_session_write_requires_existing_env_and_rejects_line_injection(tmp_path):
    missing = tmp_path / "missing.env"
    with pytest.raises(FileNotFoundError):
        save_string_session_to_env(missing, "fixture-session")
    assert not missing.exists()

    env_path = tmp_path / ".env"
    env_path.write_text("TELEGRAM_API_ID=12345\n", encoding="utf-8")
    with pytest.raises(ValueError, match="single-line"):
        save_string_session_to_env(env_path, "fixture-session\nOTHER=unsafe")
    assert env_path.read_text(encoding="utf-8") == "TELEGRAM_API_ID=12345\n"
