"""Plain-text cleanup for FeedItem fields after raw_payload mapping."""

from __future__ import annotations

import html
import re
from typing import Any

from bs4 import BeautifulSoup

_HTML_TAG = re.compile(r"<[a-zA-Z/!][^>]*>")
_WHITESPACE = re.compile(r"[ \t\f\v]+")


def clean_text(value: Any) -> str:
    """Strip HTML, unescape entities, and collapse noisy whitespace. Keep paragraph breaks."""
    if not isinstance(value, str) or not value:
        return ""
    text = html.unescape(value.replace("\xa0", " "))
    if _HTML_TAG.search(text):
        text = BeautifulSoup(text, "html.parser").get_text(separator=" ")
        text = html.unescape(text.replace("\xa0", " "))
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [_WHITESPACE.sub(" ", line).strip() for line in text.split("\n")]
    collapsed: list[str] = []
    blank = False
    for line in lines:
        if not line:
            if collapsed and not blank:
                collapsed.append("")
            blank = True
            continue
        collapsed.append(line)
        blank = False
    return "\n".join(collapsed).strip()


def clean_oneline(value: Any) -> str | None:
    """Single-line cleaned text, or None when empty."""
    text = " ".join(clean_text(value).split())
    return text or None


def clip(value: str | None, size: int) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    if not stripped:
        return None
    return stripped[:size]
