"""Детерминированное извлечение структурных сущностей
из нормализованного FeedItem."""

from __future__ import annotations

import html
import ipaddress
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

# В Siftly правило пути терялось при извлечении
# только hostname.
_PATH_TOOL_RULES: tuple[tuple[str, str, str], ...] = (
    ("github.com", "/features/copilot", "GitHub Copilot"),
)

_HTML_CODE_RE = re.compile(r"(?is)<(?P<tag>code|pre)\b[^>]*>.*?</(?P=tag)\s*>")
_FENCE_OPEN_RE = re.compile(r"^[ \t]{0,3}(?P<fence>`{3,}|~{3,})[^\r\n]*(?:\r?\n|$)")
_INLINE_CODE_RE = re.compile(
    r"(?<!\\)(?<!`)(?P<delimiter>`+)(?!`).*?(?<!`)(?P=delimiter)(?!`)",
    re.DOTALL,
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
        discovery_text, url_text = _prepare_text(text)
        _extend_unique(hashtags, hashtag_keys, _text_hashtags(discovery_text), key=str.casefold)
        _extend_unique(
            mentions,
            mention_keys,
            _text_mentions(discovery_text, source_type),
            key=str.casefold,
        )
        _extend_urls(urls, url_keys, _text_urls(url_text))

    # Учитываются явные списки; поля Habr `tags` и `hubs`
    # отдельно не являются хештегами.
    for value in _strings(raw_payload.get("hashtags")):
        discovery_text, _ = _prepare_text(value)
        _extend_unique(hashtags, hashtag_keys, _text_hashtags(discovery_text), key=str.casefold)
        tag = _explicit_hashtag(value)
        if tag is not None:
            _extend_unique(hashtags, hashtag_keys, (tag,), key=str.casefold)
    for value in _strings(raw_payload.get("mentions")):
        discovery_text, _ = _prepare_text(value)
        _extend_unique(
            mentions,
            mention_keys,
            _text_mentions(discovery_text, source_type),
            key=str.casefold,
        )
    for value in _strings(raw_payload.get("urls")):
        _, url_text = _prepare_text(value)
        _extend_urls(urls, url_keys, _text_urls(url_text))

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


def _mask_content(value: str) -> str:
    return "".join(
        "\n" if character == "\n" else "\r" if character == "\r" else " "
        for character in value
    )


def _mask_fenced_code(text: str) -> str:
    lines = text.splitlines(keepends=True)
    if not lines:
        return text

    offsets: list[int] = []
    offset = 0
    for line in lines:
        offsets.append(offset)
        offset += len(line)

    masked = list(text)
    index = 0
    while index < len(lines):
        opening = _FENCE_OPEN_RE.match(lines[index])
        if opening is None:
            index += 1
            continue

        fence = opening.group("fence")
        closing_index = None
        for candidate_index in range(index + 1, len(lines)):
            candidate = lines[candidate_index].rstrip("\r\n")
            indentation = len(candidate) - len(candidate.lstrip(" "))
            marker = candidate[indentation:].rstrip(" ")
            if (
                indentation <= 3
                and len(marker) >= len(fence)
                and marker
                and all(character == fence[0] for character in marker)
            ):
                closing_index = candidate_index
                break

        end_index = closing_index if closing_index is not None else len(lines) - 1
        start_offset = offsets[index]
        end_offset = offsets[end_index] + len(lines[end_index])
        for position in range(start_offset, end_offset):
            if text[position] not in "\r\n":
                masked[position] = " "
        index = end_index + 1

    return "".join(masked)


def _mask_code(text: str) -> str:
    text = _HTML_CODE_RE.sub(lambda match: _mask_content(match.group()), text)
    text = _mask_fenced_code(text)
    return _INLINE_CODE_RE.sub(lambda match: _mask_content(match.group()), text)


def _is_escaped(text: str, index: int) -> bool:
    backslashes = 0
    index -= 1
    while index >= 0 and text[index] == "\\":
        backslashes += 1
        index -= 1
    return backslashes % 2 == 1


def _find_markdown_label_end(text: str, start: int) -> int | None:
    depth = 1
    index = start + 1
    while index < len(text):
        if _is_escaped(text, index):
            index += 1
            continue
        if text[index] == "[":
            depth += 1
        elif text[index] == "]":
            depth -= 1
            if depth == 0:
                return index
        index += 1
    return None


def _parse_markdown_link(text: str, label_end: int) -> tuple[str, int] | None:
    opening = label_end + 1
    if opening >= len(text) or text[opening] != "(":
        return None

    index = opening + 1
    while index < len(text) and text[index].isspace():
        index += 1
    if index >= len(text):
        return None

    if text[index] == "<":
        target_start = index + 1
        index = target_start
        while index < len(text) and text[index] != ">":
            if text[index] in "\r\n":
                return None
            if _is_escaped(text, index):
                index += 1
            index += 1
        if index >= len(text):
            return None
        target = text[target_start:index]
        index += 1
    else:
        target_start = index
        depth = 0
        while index < len(text):
            if _is_escaped(text, index):
                index += 1
            elif text[index] == "(":
                depth += 1
            elif text[index] == ")":
                if depth == 0:
                    return text[target_start:index], index + 1
                depth -= 1
            elif text[index].isspace() and depth == 0:
                break
            index += 1
        target = text[target_start:index]

    while index < len(text) and text[index].isspace():
        index += 1
    if index < len(text) and text[index] == ")":
        return target, index + 1

    if index < len(text) and text[index] in "\"'":
        quote = text[index]
        index += 1
        while index < len(text):
            if _is_escaped(text, index):
                index += 1
            elif text[index] == quote:
                index += 1
                break
            index += 1
    elif index < len(text) and text[index] == "(":
        depth = 1
        index += 1
        while index < len(text) and depth:
            if _is_escaped(text, index):
                index += 1
            elif text[index] == "(":
                depth += 1
            elif text[index] == ")":
                depth -= 1
            index += 1
        if depth:
            return None
    else:
        return None

    while index < len(text) and text[index].isspace():
        index += 1
    if index >= len(text) or text[index] != ")":
        return None
    return target, index + 1


def _unescape_markdown(value: str) -> str:
    result: list[str] = []
    index = 0
    while index < len(value):
        if (
            value[index] == "\\"
            and index + 1 < len(value)
            and not value[index + 1].isalnum()
            and not value[index + 1].isspace()
        ):
            index += 1
        result.append(value[index])
        index += 1
    return "".join(result)


def _same_url_label(label: str, target: str) -> bool:
    label_url = _normalize_url(_unescape_markdown(label.strip()))
    target_url = _normalize_url(_unescape_markdown(target.strip()))
    return (
        label_url is not None
        and target_url is not None
        and _url_identity(label_url) == _url_identity(target_url)
    )


def _prepare_text(text: str) -> tuple[str, str]:
    text = _mask_code(text)
    entity_text = list(text)
    url_text: list[str] = []
    text_cursor = 0
    index = 0
    while index < len(text):
        if text[index] != "[" or _is_escaped(text, index):
            index += 1
            continue
        label_end = _find_markdown_label_end(text, index)
        if label_end is None:
            index += 1
            continue
        link = _parse_markdown_link(text, label_end)
        if link is None:
            index = label_end + 1
            continue
        target, link_end = link
        label = text[index + 1 : label_end]
        prefix_end = index + 1 if _same_url_label(label, target) else label_end
        url_text.extend(
            (text[text_cursor:prefix_end], " ", _unescape_markdown(target), " ")
        )
        text_cursor = link_end
        for position in range(label_end, link_end):
            if text[position] not in "\r\n":
                entity_text[position] = " "
        index = link_end
    url_text.append(text[text_cursor:])
    return "".join(entity_text), "".join(url_text)


def _text_hashtags(text: str) -> Iterable[str]:
    url_spans = tuple(
        (match.start(), match.end())
        for match in _URL_RE.finditer(text)
        if _normalize_url(match.group(0)) is not None
    )
    index = 0
    while index < len(text):
        if _inside_spans(index, url_spans):
            index += 1
            continue
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
    url_spans = tuple(
        (match.start(), match.end())
        for match in _URL_RE.finditer(text)
        if _normalize_url(match.group(0)) is not None
    )
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
            if not _is_mention_boundary_blocker(previous) and start < len(text):
                end = start
                while end < len(text) and text[end].isascii() and (
                    text[end].isalnum() or text[end] in "._-"
                ):
                    end += 1
                candidate_end = end
                while end > start and text[end - 1] == ".":
                    end -= 1
                username = text[start:end]
                valid_username = re.fullmatch(r"[A-Za-z0-9_-]{3,20}", username) is not None
                if candidate_end < len(text) and (
                    text[candidate_end].isalnum() or text[candidate_end] in "._-/"
                ):
                    valid_username = False
                if valid_username:
                    yield f"u/{username}"
                    index = candidate_end
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
    allow_bare_tool_host = False
    if candidate.casefold().startswith("www."):
        candidate = f"https://{candidate}"
    elif not candidate.casefold().startswith(("http://", "https://")):
        if not _BARE_URL_RE.fullmatch(candidate):
            return None
        candidate = f"https://{candidate}"
        allow_bare_tool_host = True
    try:
        parsed = urlsplit(candidate)
        hostname = parsed.hostname
        _ = parsed.port  # Проверить диапазон порта.
    except ValueError:
        return None
    normalized_hostname = hostname.casefold().rstrip(".") if hostname else ""
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or not hostname
        or not _valid_hostname(hostname)
        or any(character.isspace() or ord(character) < 32 for character in candidate)
    ):
        return None
    if allow_bare_tool_host and not any(
        normalized_hostname == domain or normalized_hostname.endswith(f".{domain}")
        for domain in _TOOL_DOMAINS
    ):
        return None
    return urlunsplit(
        (parsed.scheme.casefold(), parsed.netloc, parsed.path, parsed.query, parsed.fragment)
    )


def _valid_hostname(hostname: str) -> bool:
    try:
        ipaddress.ip_address(hostname)
        return True
    except ValueError:
        if ":" in hostname:
            return False

    domain = hostname[:-1] if hostname.endswith(".") else hostname
    if not domain:
        return False
    try:
        ascii_domain = domain.encode("idna").decode("ascii")
    except UnicodeError:
        return False
    if len(ascii_domain) > 253:
        return False

    labels = ascii_domain.split(".")
    return all(
        label
        and len(label) <= 63
        and label[0].isalnum()
        and label[-1].isalnum()
        and all(
            character.isascii() and (character.isalnum() or character == "-")
            for character in label
        )
        for label in labels
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
