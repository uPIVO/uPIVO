# Local Telegram collector

This is a local Telethon connector and console runner. It does not persist posts or cursors to PostgreSQL, SQLite, or files; it does not use Celery or AI processing. The caller supplies the incremental message ID and owns any future persistence, deduplication, and cursor updates.

## Credentials and session

Each developer uses their own Telegram API credentials. Obtain `TELEGRAM_API_ID` and `TELEGRAM_API_HASH` from [my.telegram.org](https://my.telegram.org) and put them in the repository-root `.env` (create it from `.env.example` first). Never commit, paste, or log these values. `.env` is ignored by Git.

From `backend/`, generate a Telethon StringSession:

```bash
uv run python -m scripts.generate_telegram_session
```

The helper prompts for the phone number, Telegram login code, and (if requested) 2FA password. It saves the StringSession directly into the existing root `.env`, preserves other settings, restricts the file to owner access where supported, and never prints the session. Do not share or check in the generated session; it is equivalent to account credentials. The connector never starts an interactive login: generate the session first.

## Read channel posts

Run commands from `backend/` after dependencies are installed. The source can be a channel username (`@example` or `example`), a channel URL (`https://t.me/example` or `https://t.me/s/example`), a full Telethon marked channel ID such as `-1001234567890`, or a private `https://t.me/c/<id>` link for a channel the account already belongs to.

Initial collection returns the latest 50 logical posts by default; `--limit` changes that logical-post count. Album messages count as one logical post:

```bash
uv run python -m scripts.telegram_sync @example --limit 50
```

Incremental collection requires a caller-supplied cursor and returns **all** logical posts after that message ID. `--limit` applies only to the initial latest-post request. The console prints the highest returned message ID as a value the caller may persist elsewhere; the connector does not retain or advance it.

```bash
uv run python -m scripts.telegram_sync @example --after-message-id 104
```

Output is chronological. Each `TelegramPost` has a stable `external_id` (`message:<id>` or `album:<grouped_id>`), the maximum member `message_id`, ordered `message_ids`, UTC-aware `published_at`, combined captions/text, `grouped_id`, and `is_forward`. Service messages are skipped. A media post without text is returned with `text=""` (shown as `[textless post]`); media is not downloaded, and no post is sent to AI processing.

Only channels the authenticated account can access are readable. Private channels must already be joined by that account. When a signed private channel ID is not in Telethon's entity cache, the connector checks the account's existing dialogs; it never accepts invite links or performs join requests. A resolution/access error is returned clearly without logging the raw Telegram exception, channel input, API hash, phone code, or session.

## Edits and deletions

This console-only slice cannot rescan the last 10 saved logical posts because it has no saved-post history. Edit-window rescanning is deferred until a future caller supplies stored post/message IDs (and album member IDs); no local state is synthesized here. Repeated fetches can return the same stable IDs, so a future storage caller must perform idempotent upserts and update its own cursor only after its own successful work. No collected record is locally deleted by this runner, because it stores no records.

## Focused tests

From `backend/`:

```bash
pytest -q tests/test_telegram_connector.py tests/test_telegram_session.py tests/test_telegram_sync_cli.py
```

These use a fake Telegram client and temporary `.env` fixtures; they do not make Telegram requests or inspect the developer's real `.env`.
