"""HMAC-SHA256 bearer token. Not a JWT and not a server-side session row."""

import hashlib
import hmac
import json
import time
from base64 import urlsafe_b64decode, urlsafe_b64encode


class InvalidTokenError(ValueError):
    """The token is missing, altered, or expired."""


def issue_token(*, user_id: int, secret: str, ttl_seconds: int, now: int | None = None) -> str:
    if user_id < 1:
        raise ValueError("user_id must be positive")
    if not secret:
        raise ValueError("auth token secret is empty")
    if ttl_seconds < 1:
        raise ValueError("ttl_seconds must be positive")
    issued_at = int(time.time()) if now is None else now
    payload = {"sub": user_id, "exp": issued_at + ttl_seconds}
    body = _b64(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signature = hmac.new(secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest()
    return f"{body}.{_b64(signature)}"


def read_user_id(token: str, secret: str, *, now: int | None = None) -> int:
    """Return the users.id carried by a token issued with the same secret."""
    if not secret:
        raise InvalidTokenError("auth token secret is empty")
    if not token.isascii() or len(token) > 4096:
        raise InvalidTokenError("malformed token")
    body, separator, signature = token.partition(".")
    if not separator or not body or not signature:
        raise InvalidTokenError("malformed token")
    expected = hmac.new(secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest()
    try:
        actual = _b64decode(signature)
    except ValueError as exc:
        raise InvalidTokenError("malformed token") from exc
    if not hmac.compare_digest(actual, expected):
        raise InvalidTokenError("bad signature")
    try:
        payload = json.loads(_b64decode(body))
        user_id = payload["sub"]
        expires_at = payload["exp"]
    except (KeyError, TypeError, ValueError) as exc:
        raise InvalidTokenError("malformed token") from exc
    if not isinstance(user_id, int) or isinstance(user_id, bool) or user_id < 1:
        raise InvalidTokenError("malformed token")
    if not isinstance(expires_at, int) or isinstance(expires_at, bool):
        raise InvalidTokenError("malformed token")
    current = int(time.time()) if now is None else now
    if expires_at <= current:
        raise InvalidTokenError("expired token")
    return user_id


def _b64(raw: bytes) -> str:
    return urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return urlsafe_b64decode(value + padding)
