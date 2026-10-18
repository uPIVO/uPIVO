import json

from sqlalchemy.exc import SQLAlchemyError

import app.processing.extractors.__main__ as extractor_cli
from app.models.enums import ProcessingStatus, SourceType
from app.models.feed_item import FeedItem
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


class _FakeResult:
    def __init__(self, rows):
        self.rows = rows

    def all(self):
        return self.rows

    def first(self):
        return self.rows[0] if self.rows else None


class _FakeNoAutoflush:
    def __init__(self, session):
        self.session = session

    def __enter__(self):
        self.session.no_autoflush_calls += 1
        return None

    def __exit__(self, *_exc_info):
        return False


class _FakeSession:
    def __init__(self, rows=(), error=None):
        self.rows = list(rows)
        self.error = error
        self.statements = []
        self.commit_calls = 0
        self.flush_calls = 0
        self.close_calls = 0
        self.no_autoflush_calls = 0
        self.no_autoflush = _FakeNoAutoflush(self)

    def __enter__(self):
        return self

    def __exit__(self, *_exc_info):
        self.close_calls += 1

    def execute(self, statement):
        self.statements.append(statement)
        if self.error is not None:
            raise self.error
        return _FakeResult(self.rows)

    def commit(self):
        self.commit_calls += 1

    def flush(self):
        self.flush_calls += 1


def test_fixture_cli_is_explicit_offline_mode_and_displays_inputs(tmp_path, capsys, monkeypatch):
    fixture = tmp_path / "feed-item.json"
    fixture.write_text(
        json.dumps(
            {
                "source_type": "telegram",
                "title": "Заголовок #Тест",
                "body_text": "Текст https://example.org",
                "original_url": "https://t.me/channel/1",
                "raw_payload": {"text": "raw"},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    def unexpected_database_access():
        raise AssertionError("fixture mode must not open a database session")

    monkeypatch.setattr(extractor_cli, "_open_session", unexpected_database_access)

    assert extractor_cli.main(["--fixture", str(fixture)]) == 0

    output = capsys.readouterr().out
    assert "Входные данные" in output
    assert '"source_type": "telegram"' in output
    assert '"original_url": "https://t.me/channel/1"' in output
    assert '"raw_payload": {' in output
    assert "Результат извлечения" in output
    assert '"hashtags": [\n    "тест"' in output
    assert '"urls": [\n    "https://example.org"' in output


def test_cli_selects_normalized_database_item_read_only_and_uses_joined_source_type(
    monkeypatch, capsys
):
    item = _item(
        title="Существующая публикация",
        body_text="Ответ u/Алиса #Проект.",
        original_url="https://reddit.com/r/test/comments/1/post",
        raw_payload={"url": "https://github.com/example"},
        extracted_entities={"keep": True},
    )
    item.id = 42
    session = _FakeSession([(item, SourceType.REDDIT)])
    monkeypatch.setattr(extractor_cli, "_open_session", lambda: session)
    prompts = []

    def select_item(prompt):
        prompts.append(prompt)
        return "42"

    assert extractor_cli.main([], input_fn=select_item) == 0

    output = capsys.readouterr().out
    assert "42\treddit\tСуществующая публикация" in output
    assert '"source_type": "reddit"' in output
    assert '"title": "Существующая публикация"' in output
    assert '"body_text": "Ответ u/Алиса #Проект."' in output
    assert '"original_url": "https://reddit.com/r/test/comments/1/post"' in output
    assert '"raw_payload": {\n    "url": "https://github.com/example"' in output
    assert '"mentions": [\n    "u/Алиса"' in output
    assert prompts == ["ID публикации для проверки: "]
    statement = session.statements[0]
    assert str(statement).lstrip().startswith("SELECT")
    assert "sources.type" in str(statement)
    assert ProcessingStatus.NORMALIZED in statement.compile().params.values()
    assert item.extracted_entities == {"keep": True}
    assert item.body_text == "Ответ u/Алиса #Проект."
    assert session.commit_calls == 0
    assert session.flush_calls == 0
    assert session.no_autoflush_calls == 1
    assert session.close_calls == 1


def test_cli_item_id_reads_normalized_item_without_prompt(monkeypatch, capsys):
    item = _item(
        title="Видео",
        body_text="Описание публикации",
        raw_payload={"video_id": "abcdefghijk"},
    )
    item.id = 43
    session = _FakeSession([(item, SourceType.YOUTUBE)])
    monkeypatch.setattr(extractor_cli, "_open_session", lambda: session)

    def unexpected_prompt(_prompt):
        raise AssertionError("--item-id must not prompt for input")

    assert extractor_cli.main(["--item-id", "43"], input_fn=unexpected_prompt) == 0

    output = capsys.readouterr().out
    assert '"source_type": "youtube"' in output
    assert '"has_media": true' in output
    assert "Нормализованные публикации (последние" not in output
    assert session.commit_calls == 0
    assert session.flush_calls == 0
    assert session.no_autoflush_calls == 1
    assert session.close_calls == 1


def test_cli_rejects_id_not_in_recent_selection(monkeypatch, capsys):
    item = _item(title="Публикация")
    item.id = 42
    session = _FakeSession([(item, SourceType.TELEGRAM)])
    monkeypatch.setattr(extractor_cli, "_open_session", lambda: session)

    assert extractor_cli.main([], input_fn=lambda _prompt: "404") == 2

    assert "отсутствует в списке" in capsys.readouterr().err.lower()
    assert item.extracted_entities is None
    assert session.commit_calls == 0
    assert session.flush_calls == 0
    assert session.no_autoflush_calls == 1
    assert session.close_calls == 1


def test_cli_item_id_reports_missing_or_unnormalized_item(monkeypatch, capsys):
    session = _FakeSession()
    monkeypatch.setattr(extractor_cli, "_open_session", lambda: session)

    assert extractor_cli.main(["--item-id", "404"], input_fn=lambda _prompt: "unexpected") == 2

    assert "не найден" in capsys.readouterr().err.lower()
    assert session.commit_calls == 0
    assert session.flush_calls == 0
    assert session.no_autoflush_calls == 1
    assert session.close_calls == 1


def test_cli_handles_empty_normalized_list_and_database_errors(monkeypatch, capsys):
    empty_session = _FakeSession()
    monkeypatch.setattr(extractor_cli, "_open_session", lambda: empty_session)
    assert extractor_cli.main([], input_fn=lambda _prompt: "unexpected") == 0
    output = capsys.readouterr().out.lower()
    assert "нет нормализованных публикаций" in output
    assert empty_session.no_autoflush_calls == 1
    assert empty_session.close_calls == 1

    failed_session = _FakeSession(error=SQLAlchemyError("database unavailable"))
    monkeypatch.setattr(extractor_cli, "_open_session", lambda: failed_session)
    assert extractor_cli.main(["--item-id", "1"]) == 1
    assert "ошибка чтения" in capsys.readouterr().err.lower()
    assert failed_session.no_autoflush_calls == 1
    assert failed_session.close_calls == 1
    assert failed_session.commit_calls == 0
    assert failed_session.flush_calls == 0
