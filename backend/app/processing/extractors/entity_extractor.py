"""Детерминированное извлечение структурных сущностей
из нормализованного FeedItem."""

from __future__ import annotations

import html
import re
import unicodedata
from collections.abc import Callable, Iterable, Mapping
from typing import TypedDict
from urllib.parse import urlsplit, urlunsplit

from app.models.enums import SourceType
from app.models.feed_item import FeedItem


class ExtractedEntities(TypedDict):
    hashtags: list[str]
    mentions: list[str]
    urls: list[str]
    tools: list[str]
    has_media: bool | None
    media_types: list[str]


# Справочник адаптирован из Siftly/lib/rawjson-extractor.ts.
# Copyright (c) 2025 Siftly Contributors.
# Лицензия MIT сохранена в SIFTLY-LICENSE.txt.
_TOOL_DOMAINS: dict[str, str] = {
    # Разработка
    "github.com": "GitHub",
    "gitlab.com": "GitLab",
    "bitbucket.org": "Bitbucket",
    "stackoverflow.com": "Stack Overflow",
    "replit.com": "Replit",
    "codepen.io": "CodePen",
    "codesandbox.io": "CodeSandbox",
    "stackblitz.com": "StackBlitz",
    "glitch.com": "Glitch",
    "npmjs.com": "npm",
    "pypi.org": "PyPI",
    "crates.io": "crates.io",
    "docker.com": "Docker",
    "hub.docker.com": "Docker Hub",
    "vercel.com": "Vercel",
    "netlify.com": "Netlify",
    "railway.app": "Railway",
    "render.com": "Render",
    "fly.io": "Fly.io",
    "supabase.com": "Supabase",
    "planetscale.com": "PlanetScale",
    "neon.tech": "Neon",
    "turso.tech": "Turso",
    "cloudflare.com": "Cloudflare",
    "aws.amazon.com": "AWS",
    "console.aws.amazon.com": "AWS",
    "cloud.google.com": "Google Cloud",
    "azure.microsoft.com": "Azure",
    "linear.app": "Linear",
    "jira.atlassian.com": "Jira",
    "atlassian.com": "Atlassian",
    # Искусственный интеллект и машинное обучение
    "huggingface.co": "HuggingFace",
    "arxiv.org": "arxiv",
    "openai.com": "OpenAI",
    "anthropic.com": "Anthropic",
    "replicate.com": "Replicate",
    "perplexity.ai": "Perplexity",
    "midjourney.com": "Midjourney",
    "runwayml.com": "Runway",
    "elevenlabs.io": "ElevenLabs",
    "lmsys.org": "LMSys",
    "together.ai": "Together AI",
    "groq.com": "Groq",
    "mistral.ai": "Mistral",
    "cohere.com": "Cohere",
    "stability.ai": "Stability AI",
    "deepmind.google": "DeepMind",
    "colab.research.google.com": "Google Colab",
    "kaggle.com": "Kaggle",
    "wandb.ai": "Weights & Biases",
    "modal.com": "Modal",
    "fireworks.ai": "Fireworks AI",
    "anyscale.com": "Anyscale",
    "cursor.sh": "Cursor",
    "cursor.com": "Cursor",
    "v0.dev": "v0",
    "bolt.new": "Bolt",
    "lovable.dev": "Lovable",
    "devin.ai": "Devin",
    # Дизайн
    "figma.com": "Figma",
    "framer.com": "Framer",
    "dribbble.com": "Dribbble",
    "behance.net": "Behance",
    "canva.com": "Canva",
    "spline.design": "Spline",
    "lottiefiles.com": "LottieFiles",
    # Продуктивность
    "notion.so": "Notion",
    "obsidian.md": "Obsidian",
    "roamresearch.com": "Roam Research",
    "logseq.com": "Logseq",
    "airtable.com": "Airtable",
    "coda.io": "Coda",
    "miro.com": "Miro",
    "loom.com": "Loom",
    "cal.com": "Cal.com",
    # Медиа и контент
    "youtube.com": "YouTube",
    "youtu.be": "YouTube",
    "substack.com": "Substack",
    "medium.com": "Medium",
    "producthunt.com": "Product Hunt",
    "app.daily.dev": "daily.dev",
    "hackernews.com": "Hacker News",
    "news.ycombinator.com": "Hacker News",
    "dev.to": "dev.to",
    "hashnode.com": "Hashnode",
    "beehiiv.com": "Beehiiv",
    # Сообщества
    "discord.com": "Discord",
    "discord.gg": "Discord",
    "slack.com": "Slack",
    "reddit.com": "Reddit",
    "telegram.org": "Telegram",
    "t.me": "Telegram",
    # Финансы и криптовалюты
    "coinbase.com": "Coinbase",
    "binance.com": "Binance",
    "uniswap.org": "Uniswap",
    "opensea.io": "OpenSea",
    "dune.com": "Dune Analytics",
    "etherscan.io": "Etherscan",
    "solscan.io": "Solscan",
    "defillama.com": "DefiLlama",
}

# В Siftly правило пути терялось при извлечении только hostname.
_PATH_TOOL_RULES: tuple[tuple[str, str, str], ...] = (
    ("github.com", "/features/copilot", "GitHub Copilot"),
)

_URL_RE = re.compile(
    r"(?i)(?:https?://|www\.)[^\s<>\"'`]+|"
    r"(?<![\w@])(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,}"
    r"(?::\d+)?(?:[/?#][^\s<>\"'`]*)?"
)
_BARE_URL_RE = re.compile(
    r"(?i)(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,}"
    r"(?::\d+)?(?:[/?#][^\s<>\"'`]*)?"
)
_HANDLE_CHARS = frozenset("_.-")
_TRAILING_URL_PUNCTUATION = frozenset(".,;:!?…")
_MEDIA_TYPE_ALIASES = {
    "photo": "photo",
    "image": "photo",
    "video": "video",
    "gif": "gif",
    "animated_gif": "gif",
    "animated-gif": "gif",
    "animatedgif": "gif",
}
_YOUTUBE_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


def extract_entities(item: FeedItem, source_type: SourceType) -> ExtractedEntities:
    """Вернуть сущности FeedItem без изменения объекта
    и внешних эффектов.
    """
    raw_payload = item.raw_payload if isinstance(item.raw_payload, dict) else {}
    text_values: list[str] = []
    for value in (item.title, item.body_text):
        if isinstance(value, str) and value:
            text_values.append(value)
    if source_type is SourceType.YOUTUBE:
        for key in ("description", "transcript"):
            value = raw_payload.get(key)
            if isinstance(value, str) and value:
                text_values.append(value)

    hashtags: list[str] = []
    hashtag_keys: set[str] = set()
    mentions: list[str] = []
    mention_keys: set[str] = set()
    urls: list[str] = []
    url_keys: set[str] = set()

    for text in text_values:
        _extend_unique(hashtags, hashtag_keys, _text_hashtags(text), key=str.casefold)
        _extend_unique(
            mentions,
            mention_keys,
            _text_mentions(text, source_type),
            key=str.casefold,
        )
        _extend_urls(urls, url_keys, _text_urls(text))

    # Учитываются явные списки; поля Habr `tags` и `hubs` не являются хештегами.
    for value in _strings(raw_payload.get("hashtags")):
        _extend_unique(hashtags, hashtag_keys, _text_hashtags(value), key=str.casefold)
        tag = _explicit_hashtag(value)
        if tag is not None:
            _extend_unique(hashtags, hashtag_keys, (tag,), key=str.casefold)
    for value in _strings(raw_payload.get("mentions")):
        _extend_unique(
            mentions,
            mention_keys,
            _text_mentions(value, source_type),
            key=str.casefold,
        )
    for value in _strings(raw_payload.get("urls")):
        _extend_urls(urls, url_keys, _text_urls(value))

    if source_type is SourceType.REDDIT:
        self_links = _self_link_keys(item, raw_payload)
        for key in ("url", "url_overridden_by_dest"):
            value = raw_payload.get(key)
            if isinstance(value, str):
                for url, identity in _normalized_urls(value):
                    if identity not in self_links:
                        _append_url(urls, url_keys, url, identity)

    media_types: list[str] = []
    media_type_keys: set[str] = set()
    for value in _strings(raw_payload.get("media_types")):
        media_type = _normalize_media_type(value)
        if media_type is not None:
            _extend_unique(media_types, media_type_keys, (media_type,))
    explicit_has_media = raw_payload.get("has_media")
    has_media = explicit_has_media if isinstance(explicit_has_media, bool) else None

    if source_type is SourceType.YOUTUBE:
        video_id = raw_payload.get("video_id")
        if isinstance(video_id, str) and _YOUTUBE_VIDEO_ID_RE.fullmatch(video_id):
            _extend_unique(media_types, media_type_keys, ("video",))
            has_media = True
    elif source_type is SourceType.REDDIT:
        for media_type in _reddit_media_types(raw_payload):
            _extend_unique(media_types, media_type_keys, (media_type,))
        if media_types:
            has_media = True

    if media_types:
        has_media = True

    tools: list[str] = []
    tool_keys: set[str] = set()
    for url in urls:
        tool = _tool_for_url(url)
        if tool is not None:
            _extend_unique(tools, tool_keys, (tool,))

    return {
        "hashtags": hashtags,
        "mentions": mentions,
        "urls": urls,
        "tools": tools,
        "has_media": has_media,
        "media_types": media_types,
    }


def apply_extracted_entities(item: FeedItem, source_type: SourceType) -> ExtractedEntities:
    """Присвоить только extracted_entities.

    Транзакцией управляет вызывающий код.
    """
    result = extract_entities(item, source_type)
    item.extracted_entities = result
    return result


def _text_hashtags(text: str) -> Iterable[str]:
    index = 0
    while index < len(text):
        if text[index] != "#" or (index > 0 and _is_word_or_mark(text[index - 1])):
            index += 1
            continue
        start = index + 1
        if start >= len(text) or not _is_word_or_mark(text[start]):
            index += 1
            continue
        end = start + 1
        while end < len(text) and _is_hashtag_char(text[end]):
            end += 1
        tag = unicodedata.normalize("NFC", text[start:end]).casefold()
        if tag:
            yield tag
        index = end


def _text_mentions(text: str, source_type: SourceType) -> Iterable[str]:
    url_spans = tuple((match.start(), match.end()) for match in _URL_RE.finditer(text))
    index = 0
    while index < len(text):
        if _inside_spans(index, url_spans):
            index += 1
            continue
        if text[index] == "@":
            previous = text[index - 1] if index else ""
            start = index + 1
            if (
                not _is_mention_boundary_blocker(previous)
                and start < len(text)
                and _is_word_or_mark(text[start])
            ):
                end = start + 1
                while end < len(text) and _is_handle_char(text[end]):
                    end += 1
                while end > start and text[end - 1] in ".-":
                    end -= 1
                if end > start:
                    yield f"@{text[start:end]}"
                    index = end
                    continue
        elif source_type is SourceType.REDDIT and text[index : index + 2].casefold() == "u/":
            previous = text[index - 1] if index else ""
            start = index + 2
            if (
                not _is_mention_boundary_blocker(previous)
                and start < len(text)
                and _is_word_or_mark(text[start])
            ):
                end = start + 1
                while end < len(text) and _is_handle_char(text[end]):
                    end += 1
                while end > start and text[end - 1] in ".-":
                    end -= 1
                if end > start:
                    yield f"u/{text[start:end]}"
                    index = end
                    continue
        index += 1


def _text_urls(text: str) -> Iterable[str]:
    for match in _URL_RE.finditer(text):
        url = _normalize_url(match.group(0))
        if url is not None:
            yield url


def _normalized_urls(value: str) -> Iterable[tuple[str, str]]:
    for candidate in _URL_RE.finditer(value):
        normalized = _normalize_url(candidate.group(0))
        if normalized is not None:
            yield normalized, _url_identity(normalized)


def _normalize_url(value: str) -> str | None:
    candidate = _trim_url_punctuation(html.unescape(value.strip()))
    if candidate.casefold().startswith("www."):
        candidate = f"https://{candidate}"
    elif not candidate.casefold().startswith(("http://", "https://")):
        if not _BARE_URL_RE.fullmatch(candidate):
            return None
        candidate = f"https://{candidate}"
    try:
        parsed = urlsplit(candidate)
        hostname = parsed.hostname
        _ = parsed.port  # Validate the port, including its range.
    except ValueError:
        return None
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or not hostname
        or any(character.isspace() or ord(character) < 32 for character in candidate)
    ):
        return None
    return urlunsplit(
        (parsed.scheme.casefold(), parsed.netloc, parsed.path, parsed.query, parsed.fragment)
    )


def _trim_url_punctuation(candidate: str) -> str:
    while candidate and candidate[-1] in _TRAILING_URL_PUNCTUATION:
        candidate = candidate[:-1]
    for closing, opening in ((")", "("), ("]", "["), ("}", "{")):
        while candidate.endswith(closing) and candidate.count(closing) > candidate.count(opening):
            candidate = candidate[:-1]
    return candidate


def _url_identity(url: str) -> str:
    parsed = urlsplit(url)
    hostname = (parsed.hostname or "").casefold()
    port = parsed.port
    default_port = (parsed.scheme == "http" and port == 80) or (
        parsed.scheme == "https" and port == 443
    )
    netloc = hostname if port is None or default_port else f"{hostname}:{port}"
    if parsed.username is not None:
        credentials = parsed.username
        if parsed.password is not None:
            credentials += f":{parsed.password}"
        netloc = f"{credentials}@{netloc}"
    return urlunsplit(
        (parsed.scheme.casefold(), netloc, parsed.path, parsed.query, parsed.fragment)
    )


def _self_link_keys(item: FeedItem, payload: Mapping[str, object]) -> set[str]:
    result: set[str] = set()
    candidates = (item.original_url, payload.get("permalink"), payload.get("original_url"))
    for value in candidates:
        if isinstance(value, str):
            normalized = _normalize_url(value)
            if normalized is not None:
                result.add(_url_identity(normalized))
    return result


def _tool_for_url(url: str) -> str | None:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").casefold().rstrip(".")
    path = parsed.path.rstrip("/") or "/"
    ordered_rules = sorted(_PATH_TOOL_RULES, key=lambda rule: len(rule[1]), reverse=True)
    for rule_host, rule_path, name in ordered_rules:
        if (host == rule_host or host.endswith(f".{rule_host}")) and (
            path == rule_path or path.startswith(f"{rule_path}/")
        ):
            return name
    matches = [
        (domain, name)
        for domain, name in _TOOL_DOMAINS.items()
        if host == domain or host.endswith(f".{domain}")
    ]
    if not matches:
        return None
    return max(matches, key=lambda pair: len(pair[0]))[1]


def _reddit_media_types(payload: Mapping[str, object]) -> Iterable[str]:
    if payload.get("is_video") is True:
        yield "video"
    hint = payload.get("post_hint")
    if isinstance(hint, str):
        media_type = _MEDIA_TYPE_ALIASES.get(hint.casefold())
        if media_type is None and hint.casefold() in {"hosted:video", "rich:video"}:
            media_type = "video"
        if media_type is not None:
            yield media_type
    for key in ("media", "secure_media"):
        media = payload.get(key)
        if not isinstance(media, Mapping):
            continue
        if isinstance(media.get("reddit_video"), Mapping):
            yield "video"
        oembed = media.get("oembed")
        if isinstance(oembed, Mapping):
            media_type = _MEDIA_TYPE_ALIASES.get(str(oembed.get("type", "")).casefold())
            if media_type is not None:
                yield media_type
    gallery = payload.get("gallery_data")
    metadata = payload.get("media_metadata")
    if isinstance(gallery, Mapping) and isinstance(metadata, Mapping):
        items = gallery.get("items")
        if isinstance(items, list):
            for item in items:
                if not isinstance(item, Mapping):
                    continue
                media_id = item.get("media_id")
                entry = metadata.get(media_id) if isinstance(media_id, str) else None
                if not isinstance(entry, Mapping):
                    continue
                media_type = str(entry.get("e", "")).casefold()
                if media_type == "image":
                    yield "photo"
                elif media_type == "animatedimage":
                    yield "gif"
                elif media_type == "video":
                    yield "video"


def _explicit_hashtag(value: str) -> str | None:
    tag = value.strip()
    if tag.startswith("#"):
        tag = tag[1:]
    if not tag or not _is_word_or_mark(tag[0]) or not all(_is_hashtag_char(char) for char in tag):
        return None
    return unicodedata.normalize("NFC", tag).casefold()


def _normalize_media_type(value: str) -> str | None:
    return _MEDIA_TYPE_ALIASES.get(value.strip().casefold())


def _strings(value: object) -> Iterable[str]:
    if isinstance(value, list):
        return (item for item in value if isinstance(item, str))
    return ()


def _extend_urls(urls: list[str], keys: set[str], values: Iterable[str]) -> None:
    for value in values:
        normalized = _normalize_url(value)
        if normalized is not None:
            _append_url(urls, keys, normalized, _url_identity(normalized))


def _append_url(urls: list[str], keys: set[str], url: str, identity: str) -> None:
    if identity not in keys:
        keys.add(identity)
        urls.append(url)


def _extend_unique(
    values: list[str],
    keys: set[str],
    candidates: Iterable[str],
    *,
    key: Callable[[str], str] | None = None,
) -> None:
    for value in candidates:
        identity = key(value) if key is not None else value
        if identity not in keys:
            keys.add(identity)
            values.append(value)


def _inside_spans(index: int, spans: tuple[tuple[int, int], ...]) -> bool:
    return any(start <= index < end for start, end in spans)


def _is_word_or_mark(character: str) -> bool:
    return (
        character == "_"
        or character.isalnum()
        or unicodedata.category(character).startswith("M")
    )


def _is_hashtag_char(character: str) -> bool:
    return _is_word_or_mark(character)


def _is_handle_char(character: str) -> bool:
    return _is_word_or_mark(character) or character in _HANDLE_CHARS


def _is_mention_boundary_blocker(character: str) -> bool:
    blockers = "._%+-/@"
    return bool(character) and (_is_word_or_mark(character) or character in blockers)
