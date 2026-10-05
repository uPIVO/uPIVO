"""Keyless YouTube captions via timedtext (no Data API key)."""

from __future__ import annotations

import html
import json
import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)

TIMEDTEXT_URL = "https://www.youtube.com/api/timedtext"
WATCH_URL = "https://www.youtube.com/watch"
PREFERRED_LANGS = ("ru", "en")
_PLAYER_RESPONSE_RE = re.compile(
    r"ytInitialPlayerResponse\s*=\s*(\{.+?\});\s*(?:var|</script>)",
    re.DOTALL,
)


@dataclass(frozen=True)
class CaptionTrack:
    lang_code: str
    kind: str
    name: str
    is_default: bool
    base_url: str | None = None


@dataclass(frozen=True)
class CaptionResult:
    text: str
    lang: str
    kind: str


def fetch_transcript(client: httpx.Client, video_id: str) -> CaptionResult | None:
    """Return captions when available. Missing captions or caption HTTP errors yield None."""
    try:
        tracks = _list_tracks(client, video_id)
        track = _pick_track(tracks)
        if track is None:
            return None
        text = _download_track(client, video_id, track)
        if not text:
            return None
        return CaptionResult(text=text, lang=track.lang_code, kind=track.kind or "manual")
    except Exception:
        logger.warning("Skipping captions for video %s", video_id, exc_info=True)
        return None


def _list_tracks(client: httpx.Client, video_id: str) -> list[CaptionTrack]:
    response = _try_get(client, TIMEDTEXT_URL, params={"type": "list", "v": video_id})
    tracks = _parse_list_xml(response.text) if response is not None else []
    if tracks:
        return tracks
    return _tracks_from_watch_page(client, video_id)


def _download_track(client: httpx.Client, video_id: str, track: CaptionTrack) -> str | None:
    if track.base_url:
        response = _try_get(client, track.base_url)
    else:
        params: dict[str, str] = {"v": video_id, "lang": track.lang_code}
        if track.name:
            params["name"] = track.name
        if track.kind:
            params["kind"] = track.kind
        response = _try_get(client, TIMEDTEXT_URL, params=params)
    if response is None or not response.content:
        return None
    return _parse_transcript_xml(response.text)


def _try_get(
    client: httpx.Client, url: str, *, params: dict[str, str] | None = None
) -> httpx.Response | None:
    try:
        response = client.get(url, params=params)
    except httpx.HTTPError:
        logger.info("Caption request failed for %s", url)
        return None
    if response.status_code >= 400 or not response.content:
        return None
    return response


def _parse_list_xml(payload: str) -> list[CaptionTrack]:
    root = _xml_root(payload, "transcript_list")
    if root is None:
        return []
    tracks: list[CaptionTrack] = []
    for node in root.findall("track"):
        lang = (node.attrib.get("lang_code") or "").strip()
        if not lang:
            continue
        tracks.append(
            CaptionTrack(
                lang_code=lang,
                kind=(node.attrib.get("kind") or "").strip(),
                name=(node.attrib.get("name") or "").strip(),
                is_default=node.attrib.get("lang_default") == "true",
            )
        )
    return tracks


def _parse_transcript_xml(payload: str) -> str | None:
    root = _xml_root(payload, "transcript")
    if root is None:
        return None
    lines: list[str] = []
    previous = None
    for node in root.findall("text"):
        raw = "".join(node.itertext())
        line = html.unescape(re.sub(r"<[^>]+>", " ", raw))
        line = re.sub(r"\s+", " ", line).strip()
        if not line or line == previous:
            continue
        lines.append(line)
        previous = line
    text = "\n".join(lines).strip()
    return text or None


def _xml_root(payload: str, tag: str) -> ET.Element | None:
    text = (payload or "").strip()
    if not text or tag not in text:
        return None
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return None
    if root.tag != tag:
        return None
    return root


def _pick_track(tracks: list[CaptionTrack]) -> CaptionTrack | None:
    if not tracks:
        return None
    defaults = [track for track in tracks if track.is_default]
    if defaults:
        manual = [track for track in defaults if track.kind != "asr"]
        return (manual or defaults)[0]
    for lang in PREFERRED_LANGS:
        chosen = _first_for_lang(tracks, lang)
        if chosen is not None:
            return chosen
    manual = [track for track in tracks if track.kind != "asr"]
    return (manual or tracks)[0]


def _first_for_lang(tracks: list[CaptionTrack], lang: str) -> CaptionTrack | None:
    matches = [track for track in tracks if track.lang_code.lower().startswith(lang)]
    if not matches:
        return None
    manual = [track for track in matches if track.kind != "asr"]
    return (manual or matches)[0]


def _tracks_from_watch_page(client: httpx.Client, video_id: str) -> list[CaptionTrack]:
    response = _try_get(client, WATCH_URL, params={"v": video_id})
    if response is None:
        return []
    match = _PLAYER_RESPONSE_RE.search(response.text)
    if not match:
        return []
    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError:
        return []
    renderer = (data.get("captions") or {}).get("playerCaptionsTracklistRenderer") or {}
    raw_tracks = renderer.get("captionTracks") or []
    tracks: list[CaptionTrack] = []
    for raw in raw_tracks:
        if not isinstance(raw, dict):
            continue
        lang = str(raw.get("languageCode") or "").strip()
        base_url = str(raw.get("baseUrl") or "").strip() or None
        if not lang or not base_url:
            continue
        kind = str(raw.get("kind") or "").strip()
        name = _simple_name(raw.get("name"))
        tracks.append(
            CaptionTrack(
                lang_code=lang,
                kind=kind,
                name=name,
                is_default="--d" in str(raw.get("vssId") or ""),
                base_url=base_url,
            )
        )
    return tracks


def _simple_name(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        return str(value.get("simpleText") or "").strip()
    return ""
