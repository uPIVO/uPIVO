BEGIN;

-- ============================================================
-- Upivo PostgreSQL schema
-- Current version:
-- - no AI source suggestions
-- - folders classify the common feed, not favorites
-- - feed_items stores raw_payload and extracted_entities JSONB
-- ============================================================

-- =========================
-- ENUMS
-- =========================

CREATE TYPE source_type AS ENUM (
    'telegram',
    'website',
    'rss',
    'habr',
    'youtube'
);

CREATE TYPE processing_status AS ENUM (
    'pending',
    'processing',
    'processed',
    'failed'
);

CREATE TYPE feedback_type AS ENUM (
    'useful',
    'not_interested'
);

CREATE TYPE event_type AS ENUM (
    'open_full'
);

CREATE TYPE digest_status AS ENUM (
    'generating',
    'ready',
    'failed'
);

CREATE TYPE summary_length AS ENUM (
    'short',
    'medium',
    'long'
);

-- =========================
-- USERS
-- =========================

CREATE TABLE users (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    email VARCHAR(320) NOT NULL UNIQUE,
    password_hash VARCHAR(255) NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP
);

-- =========================
-- SOURCES
-- =========================

CREATE TABLE sources (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    type source_type NOT NULL,
    name VARCHAR(255) NOT NULL,
    canonical_url TEXT NOT NULL,
    external_id VARCHAR(512),
    last_synced_at TIMESTAMP,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_sources_type_external_id
    ON sources(type, external_id);

-- =========================
-- USER <-> SOURCE
-- =========================

CREATE TABLE user_sources (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(id),
    source_id BIGINT NOT NULL REFERENCES sources(id),
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT uq_user_sources_user_source
        UNIQUE (user_id, source_id)
);

-- =========================
-- CONTENT
-- =========================

CREATE TABLE feed_items (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_id BIGINT NOT NULL REFERENCES sources(id),
    external_id VARCHAR(512) NOT NULL,
    title TEXT,
    body_text TEXT NOT NULL,
    original_url TEXT NOT NULL,
    author VARCHAR(255),
    published_at TIMESTAMP,
    collected_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,

    -- Исходный payload от Telegram/RSS/сайта и т.п.
    raw_payload JSONB,

    -- Быстро извлечённые сущности без LLM:
    -- hashtags, mentions, urls, tools/products и т.п.
    extracted_entities JSONB,

    content_hash VARCHAR(64),
    summary TEXT,
    importance_score DOUBLE PRECISION,
    -- Полные результаты анализа, отпечатки входов и их статус без секретов.
    ai_metadata JSONB,
    processing_status processing_status NOT NULL DEFAULT 'pending',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP,

    CONSTRAINT uq_feed_items_source_external
        UNIQUE (source_id, external_id)
);

CREATE INDEX idx_feed_items_content_hash
    ON feed_items(content_hash);

CREATE INDEX idx_feed_items_published_at
    ON feed_items(published_at);

CREATE INDEX idx_feed_items_source_id
    ON feed_items(source_id);

-- =========================
-- CONTENT CLUSTERS
-- =========================
-- Целевая схема варианта A для будущей интеграции кластеров.
-- Промежуточный Stage 4 ITD-17 пока сохраняет анализ только для FeedItem:
-- здесь намеренно нет content_clusters.importance_score и cluster_id в user_item_scores.

CREATE TABLE content_clusters (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    title TEXT,
    summary TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE cluster_items (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    cluster_id BIGINT NOT NULL REFERENCES content_clusters(id),
    feed_item_id BIGINT NOT NULL REFERENCES feed_items(id),
    is_primary BOOLEAN NOT NULL DEFAULT FALSE,

    CONSTRAINT uq_cluster_items_cluster_feed
        UNIQUE (cluster_id, feed_item_id)
);

CREATE INDEX idx_cluster_items_feed_item_id
    ON cluster_items(feed_item_id);

-- =========================
-- PERSONAL RANKING
-- =========================
-- Промежуточная реализация ITD-17 поддерживает только целевую единицу feed_item.
-- Будущие cluster_id и XOR-ограничение добавляются после интеграции ORM кластеров.

CREATE TABLE user_item_scores (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    feed_item_id BIGINT NOT NULL REFERENCES feed_items(id) ON DELETE CASCADE,
    relevance_score DOUBLE PRECISION,
    rank_score DOUBLE PRECISION,
    reason TEXT,
    calculated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    -- DTO релевантности и отпечатки; исходные интересы не сохраняются.
    analysis_metadata JSONB,

    CONSTRAINT ck_user_item_scores_relevance_range
        CHECK (relevance_score IS NULL OR (relevance_score >= 0.0 AND relevance_score <= 1.0)),
    CONSTRAINT uq_user_item_scores_user_feed
        UNIQUE (user_id, feed_item_id)
);

CREATE INDEX idx_user_item_scores_feed_item_id
    ON user_item_scores(feed_item_id);

-- =========================
-- EXPLICIT FEEDBACK
-- =========================

CREATE TABLE feedback (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(id),
    feed_item_id BIGINT NOT NULL REFERENCES feed_items(id),
    reaction feedback_type NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT uq_feedback_user_feed
        UNIQUE (user_id, feed_item_id)
);

CREATE INDEX idx_feedback_feed_item_id
    ON feedback(feed_item_id);

-- =========================
-- IMPLICIT FEEDBACK
-- =========================

CREATE TABLE user_item_events (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(id),
    feed_item_id BIGINT NOT NULL REFERENCES feed_items(id),
    event event_type NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_user_item_events_user_id
    ON user_item_events(user_id);

CREATE INDEX idx_user_item_events_feed_item_id
    ON user_item_events(feed_item_id);

-- =========================
-- FAVORITES
-- =========================

CREATE TABLE favorites (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(id),
    feed_item_id BIGINT NOT NULL REFERENCES feed_items(id),
    note TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT uq_favorites_user_feed
        UNIQUE (user_id, feed_item_id)
);

CREATE INDEX idx_favorites_feed_item_id
    ON favorites(feed_item_id);

-- =========================
-- FEED FOLDERS
-- =========================

CREATE TABLE folders (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(id),
    name VARCHAR(255) NOT NULL,
    description TEXT,
    parent_id BIGINT REFERENCES folders(id),
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP
);

CREATE INDEX idx_folders_user_id
    ON folders(user_id);

CREATE INDEX idx_folders_parent_id
    ON folders(parent_id);

CREATE TABLE feed_item_folders (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    folder_id BIGINT NOT NULL REFERENCES folders(id),
    feed_item_id BIGINT NOT NULL REFERENCES feed_items(id),
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT uq_feed_item_folders_folder_feed
        UNIQUE (folder_id, feed_item_id)
);

CREATE INDEX idx_feed_item_folders_feed_item_id
    ON feed_item_folders(feed_item_id);

-- =========================
-- DIGESTS
-- =========================

CREATE TABLE digests (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(id),
    period_start TIMESTAMP NOT NULL,
    period_end TIMESTAMP NOT NULL,
    item_limit INTEGER NOT NULL,
    status digest_status NOT NULL,
    generated_at TIMESTAMP,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_digests_user_id
    ON digests(user_id);

CREATE INDEX idx_digests_period
    ON digests(period_start, period_end);

CREATE TABLE digest_items (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    digest_id BIGINT NOT NULL REFERENCES digests(id),
    feed_item_id BIGINT NOT NULL REFERENCES feed_items(id),

    -- NULL, если карточка представляет одиночный FeedItem.
    cluster_id BIGINT REFERENCES content_clusters(id),

    position INTEGER NOT NULL,
    reason TEXT,

    CONSTRAINT uq_digest_items_digest_position
        UNIQUE (digest_id, position)
);

CREATE INDEX idx_digest_items_feed_item_id
    ON digest_items(feed_item_id);

CREATE INDEX idx_digest_items_cluster_id
    ON digest_items(cluster_id);

-- =========================
-- USER SETTINGS
-- =========================

CREATE TABLE user_settings (
    user_id BIGINT PRIMARY KEY REFERENCES users(id),
    digest_size INTEGER NOT NULL DEFAULT 10,
    digest_period_hours INTEGER NOT NULL DEFAULT 24,
    summary_length summary_length NOT NULL DEFAULT 'short',
    updated_at TIMESTAMP
);

-- =========================
-- NOTIFICATIONS
-- =========================

CREATE TABLE notification_schedules (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(id),
    weekday INTEGER NOT NULL CHECK (weekday BETWEEN 1 AND 7),
    notify_time TIME NOT NULL,
    enabled BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE INDEX idx_notification_schedules_user_id
    ON notification_schedules(user_id);

COMMIT;
