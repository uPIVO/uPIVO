import os
import re
import tempfile
from pathlib import Path

_SESSION_ASSIGNMENT = re.compile(r"^\s*(?:export\s+)?TELEGRAM_SESSION\s*=.*$")


def save_string_session_to_env(env_path: Path, session: str) -> None:
    """Atomically replace TELEGRAM_SESSION without printing or changing other keys."""
    if not session or "\n" in session or "\r" in session:
        raise ValueError("Telegram session must be a non-empty single-line value")
    if not env_path.is_file():
        raise FileNotFoundError("Local .env must exist before saving a Telegram session")

    original = env_path.read_text(encoding="utf-8")
    lines = original.splitlines(keepends=True)
    updated_lines: list[str] = []
    replaced = False
    for line in lines:
        if _SESSION_ASSIGNMENT.fullmatch(line.rstrip("\r\n")):
            if replaced:
                continue
            ending = "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""
            updated_lines.append(f"TELEGRAM_SESSION={session}{ending}")
            replaced = True
        else:
            updated_lines.append(line)

    if not replaced:
        separator = "" if not original or original.endswith(("\n", "\r")) else "\n"
        updated_lines.append(f"{separator}TELEGRAM_SESSION={session}\n")

    temporary_path: str | None = None
    descriptor = -1
    try:
        descriptor, temporary_path = tempfile.mkstemp(
            prefix=f".{env_path.name}.", suffix=".tmp", dir=env_path.parent
        )
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, 0o600)
        else:
            os.chmod(temporary_path, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as temporary_file:
            descriptor = -1
            temporary_file.writelines(updated_lines)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_path, env_path)
        temporary_path = None
    except OSError:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
        raise OSError("Could not securely update the local .env file") from None
    finally:
        if temporary_path is not None:
            try:
                os.unlink(temporary_path)
            except FileNotFoundError:
                pass
