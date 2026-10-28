"""scrypt password hashes. The raw password is never stored."""

import hashlib
import hmac
import secrets
from base64 import urlsafe_b64decode, urlsafe_b64encode

_N = 2**14
_R = 8
_P = 1
_DKLEN = 32


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=_N, r=_R, p=_P, dklen=_DKLEN
    )
    return f"scrypt${_N}${_R}${_P}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, password_hash: str) -> bool:
    """Return whether password matches a stored hash. Malformed hashes do not match."""
    try:
        algorithm, n_raw, r_raw, p_raw, salt_raw, digest_raw = password_hash.split("$")
        if algorithm != "scrypt":
            return False
        if (int(n_raw), int(r_raw), int(p_raw)) != (_N, _R, _P):
            return False
        salt = _b64decode(salt_raw)
        expected = _b64decode(digest_raw)
        if len(expected) != _DKLEN:
            return False
        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=int(n_raw),
            r=int(r_raw),
            p=int(p_raw),
            dklen=len(expected),
        )
    except (TypeError, ValueError):
        return False
    return hmac.compare_digest(actual, expected)


def _b64(raw: bytes) -> str:
    return urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return urlsafe_b64decode(value + padding)
