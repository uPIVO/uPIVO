"""Normalize an account email before it is stored or looked up."""

import re

_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_MAX_LENGTH = 320


def normalize_email(value: str) -> str:
    """Return a lowercase email, or raise ValueError when it cannot be stored."""
    email = value.strip().lower()
    if not email or len(email) > _MAX_LENGTH or _EMAIL_RE.fullmatch(email) is None:
        raise ValueError("Invalid email")
    return email
