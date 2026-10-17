from app.connectors.reddit.models import RedditPost


def map_post(post: RedditPost) -> dict[str, object]:
    """Преобразовать публикацию Reddit в исходные поля общей ленты."""
    return {
        "external_id": post.id,
        "title": post.title,
        "body_text": post.body_text,
        "original_url": post.permalink,
        "author": post.author,
        "published_at": post.created_utc,
        "raw_payload": post.raw_payload,
    }
