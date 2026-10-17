"""Lexical similarity of title + body. No embeddings and no LLM."""

from __future__ import annotations

import math
import re

from app.processing.text import clean_text

# Binary cosine. Title dominates so a shared subscribe-line does not glue videos.
TITLE_WEIGHT = 0.8
BODY_WEIGHT = 0.2
SIMILARITY_THRESHOLD = 0.55
MIN_SHARED_TITLE_TOKENS = 2
MIN_SHARED_BODY_TOKENS = 4
# Same article pasted under a different headline: long body overlap, titles may not match.
COPY_BODY_THRESHOLD = 0.85
COPY_BODY_MIN_SHARED = 12
# A token posted on too many rows is a weak blocker (channel boilerplate).
BLOCK_TOKEN_MAX_DOCS = 50

_TOKEN = re.compile(r"[0-9A-Za-zА-Яа-яЁё]{3,}")
_STOPWORDS = frozenset(
    {
        "the",
        "and",
        "for",
        "with",
        "from",
        "that",
        "this",
        "was",
        "were",
        "are",
        "has",
        "have",
        "been",
        "had",
        "after",
        "before",
        "into",
        "over",
        "about",
        "says",
        "said",
        "will",
        "not",
        "but",
        "you",
        "your",
        "our",
        "their",
        "they",
        "his",
        "her",
        "she",
        "who",
        "what",
        "when",
        "where",
        "why",
        "how",
        "new",
        "now",
        "just",
        "than",
        "then",
        "also",
        "more",
        "its",
        "off",
        "это",
        "как",
        "для",
        "что",
        "или",
        "при",
        "его",
        "её",
        "уже",
        "ещё",
        "еще",
        "только",
        "также",
        "этот",
        "эта",
        "эти",
        "этом",
        "все",
        "всё",
        "после",
        "перед",
        "который",
        "которая",
        "которые",
    }
)


def token_set(value: str | None) -> set[str]:
    """Content words from cleaned text. Digits and stopwords are dropped."""
    text = clean_text(value or "")
    found: set[str] = set()
    for token in _TOKEN.findall(text.lower()):
        if token in _STOPWORDS or token.isdigit():
            continue
        found.add(token)
    return found


def texts_match(
    title_a: str | None,
    body_a: str | None,
    title_b: str | None,
    body_b: str | None,
) -> bool:
    """True when two publications look like the same event.

    Both titles present:
    score = 0.8 * cosine(title tokens) + 0.2 * cosine(body tokens).
    Link when score >= 0.55 and the titles share at least 2 content words.
    A long near-copy of the body (cosine >= 0.85, 12+ shared words) also links,
    so the same article under two headlines is one story. A short repeated
    footer is not enough.

    A missing title falls back to body cosine >= 0.55 with at least 4 shared words.
    """
    titles_a = token_set(title_a)
    titles_b = token_set(title_b)
    bodies_a = token_set(body_a)
    bodies_b = token_set(body_b)
    shared_title = len(titles_a & titles_b)
    shared_body = len(bodies_a & bodies_b)
    title_score = _cosine(titles_a, titles_b)
    body_score = _cosine(bodies_a, bodies_b)
    if titles_a and titles_b:
        score = TITLE_WEIGHT * title_score + BODY_WEIGHT * body_score
        if shared_title >= MIN_SHARED_TITLE_TOKENS and score >= SIMILARITY_THRESHOLD:
            return True
        return body_score >= COPY_BODY_THRESHOLD and shared_body >= COPY_BODY_MIN_SHARED
    return shared_body >= MIN_SHARED_BODY_TOKENS and body_score >= SIMILARITY_THRESHOLD


def _cosine(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / math.sqrt(len(left) * len(right))
