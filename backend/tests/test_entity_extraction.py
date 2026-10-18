import json

from app.models.enums import ProcessingStatus, SourceType
from app.models.feed_item import FeedItem
from app.processing.extractors.__main__ import main
from app.processing.extractors.entity_extractor import apply_extracted_entities, extract_entities


def _item(**kwargs) -> FeedItem:
    values = {
        "source_id": 1,
        "external_id": "sample",
        "title": None,
        "body_text": "",
        "original_url": "https://example.invalid/post",
        "raw_payload": None,
        "summary": "keep summary",
        "importance_score": 0.5,
        "content_hash": "keep-hash",
        "processing_status": ProcessingStatus.NORMALIZED,
    }
    values.update(kwargs)
    return FeedItem(**values)


def test_telegram_text_extracts_unicode_hashtags_mentions_and_urls_without_guessing_media():
    item = _item(
        title="#ПРИВЕТ #Cafe\u0301",
        body_text=(
            "Ответ @UserName и u/not-a-reddit-mention; email user@example.com. "
            "Ссылка https://github.com/org/repo, повтор https://github.com/org/repo."
        ),
    )

    result = extract_entities(item, SourceType.TELEGRAM)

    assert result == {
        "hashtags": ["привет", "café"],
        "mentions": ["@UserName"],
        "urls": ["https://github.com/org/repo"],
        "tools": ["GitHub"],
        "has_media": None,
        "media_types": [],
    }


def test_reddit_metadata_urls_mentions_and_positive_video_metadata():
    item = _item(
        body_text=(
            "Написал u/Alice, смотрите r/Python; "
            "письмо alice@example.com"
        ),
        original_url="https://www.reddit.com/r/python/comments/abc/post/",
        raw_payload={
            "permalink": "https://www.reddit.com/r/python/comments/abc/post/",
            "url": "https://github.com/org/repo",
            "url_overridden_by_dest": "https://github.com/org/repo",
            "is_video": True,
            "media": {"reddit_video": {"fallback_url": "https://v.redd.it/abc/DASH_720.mp4"}},
        },
    )

    result = extract_entities(item, SourceType.REDDIT)

    assert result["mentions"] == ["u/Alice"]
    assert result["urls"] == ["https://github.com/org/repo"]
    assert result["tools"] == ["GitHub"]
    assert result["has_media"] is True
    assert result["media_types"] == ["video"]


def test_reddit_false_is_video_does_not_claim_no_media_and_permalink_is_not_a_url():
    permalink = "https://www.reddit.com/r/python/comments/abc/post/"
    item = _item(
        original_url=permalink,
        raw_payload={"permalink": permalink, "url": permalink, "is_video": False},
    )

    result = extract_entities(item, SourceType.REDDIT)

    assert result["urls"] == []
    assert result["has_media"] is None
    assert result["media_types"] == []

    item.body_text = permalink
    assert extract_entities(item, SourceType.REDDIT)["urls"] == [permalink]


def test_youtube_text_supplements_description_and_transcript_but_omits_item_link():
    item = _item(
        title="Обновление",
        body_text="Текст #РЕЛИЗ https://example.org/body",
        original_url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        raw_payload={
            "kind": "youtube_rss_entry",
            "video_id": "dQw4w9WgXcQ",
            "link": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "description": "Описание @Author https://example.org/description",
            "transcript": "Расшифровка #AI https://example.org/transcript",
            "thumbnail_url": "https://i.ytimg.com/vi/dQw4w9WgXcQ/hqdefault.jpg",
        },
    )

    result = extract_entities(item, SourceType.YOUTUBE)

    assert result["hashtags"] == ["релиз", "ai"]
    assert result["mentions"] == ["@Author"]
    assert result["urls"] == [
        "https://example.org/body",
        "https://example.org/description",
        "https://example.org/transcript",
    ]
    assert result["has_media"] is True
    assert result["media_types"] == ["video"]

    invalid_video = _item(raw_payload={"video_id": "short"})
    assert extract_entities(invalid_video, SourceType.YOUTUBE)["has_media"] is None


def test_habr_tags_and_hubs_are_not_promoted_to_hashtags():
    item = _item(
        body_text="Пост с #Python",
        raw_payload={"tags": ["Python", "AI"], "hubs": ["Python", "AI"]},
    )

    result = extract_entities(item, SourceType.HABR)

    assert result["hashtags"] == ["python"]


def test_markdown_html_urls_and_tool_domain_boundaries_and_path_precedence():
    item = _item(
        body_text=(
            "[Copilot](https://github.com/features/copilot) "
            "https://github.com/org/repo. https://fakegithub.com/path "
            "https://github.com.evil/path "
            '<a href="https://hub.docker.com/r/example?a=1&amp;b=2">Docker</a> '
            "или example.net/docs; invalid https://[invalid"
        )
    )

    result = extract_entities(item, SourceType.WEBSITE)

    assert result["urls"] == [
        "https://github.com/features/copilot",
        "https://github.com/org/repo",
        "https://fakegithub.com/path",
        "https://github.com.evil/path",
        "https://hub.docker.com/r/example?a=1&b=2",
        "https://example.net/docs",
    ]
    assert result["tools"] == ["GitHub Copilot", "GitHub", "Docker Hub"]
    assert extract_entities(_item(body_text="GitHub Copilot"), SourceType.WEBSITE)["tools"] == []


def test_url_fragments_are_not_hashtags_and_invalid_hosts_are_rejected():
    item = _item(
        body_text=(
            "Ссылка https://example.org/page#section "
            "и https://example.org/page/#fragment и #обычный; "
            "https://-bad.example/path https://bad_host.example/path "
            "https://example..org/path https://bad-.example/path "
            "https://-github.com/path https://example!.org/path "
            "https://bücher.example/a(b). https://example.com/a(b). "
            "https://[2001:db8::1]/path"
        )
    )

    result = extract_entities(item, SourceType.WEBSITE)

    assert result["hashtags"] == ["обычный"]
    assert result["urls"] == [
        "https://example.org/page#section",
        "https://example.org/page/#fragment",
        "https://bücher.example/a(b)",
        "https://example.com/a(b)",
        "https://[2001:db8::1]/path",
    ]
    assert result["tools"] == []


def test_empty_or_malformed_payload_keeps_text_extraction_and_explicit_lists_are_supported():
    item = _item(
        title="#Title",
        body_text="https://example.org/path",
        raw_payload=["not", "an", "object"],
    )
    first = extract_entities(item, SourceType.RSS)
    assert first["hashtags"] == ["title"]
    assert first["urls"] == ["https://example.org/path"]
    assert first["has_media"] is None

    item.raw_payload = {
        "hashtags": ["#Python"],
        "mentions": ["@Bot"],
        "urls": ["https://example.org/path", "not a URL"],
    }
    second = extract_entities(item, SourceType.RSS)
    assert second["hashtags"] == ["title", "python"]
    assert second["mentions"] == ["@Bot"]
    assert second["urls"] == ["https://example.org/path"]
    assert extract_entities(item, SourceType.RSS) == second


def test_apply_updates_only_extracted_entities_and_is_repeatable():
    item = _item(
        title="#Test",
        body_text="https://example.org",
        raw_payload={"title": "unchanged raw"},
        extracted_entities=None,
    )
    protected = {
        "summary": item.summary,
        "importance_score": item.importance_score,
        "content_hash": item.content_hash,
        "processing_status": item.processing_status,
        "raw_payload": item.raw_payload,
        "original_url": item.original_url,
    }

    first = apply_extracted_entities(item, SourceType.WEBSITE)
    second = apply_extracted_entities(item, SourceType.WEBSITE)

    assert first == second
    assert item.extracted_entities == first
    assert {key: getattr(item, key) for key in protected} == protected


def test_interactive_cli_accepts_json_payload_without_database(capsys):
    answers = iter(
        [
            "telegram",
            "Заголовок #Тест",
            "Текст https://example.org",
            "",
        ]
    )

    assert main([], input_fn=lambda _prompt: next(answers)) == 0

    output = json.loads(capsys.readouterr().out)
    assert output["hashtags"] == ["тест"]
    assert output["urls"] == ["https://example.org"]
    assert output["has_media"] is None
