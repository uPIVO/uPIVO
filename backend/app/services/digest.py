"""Service for generating, retrieving, cleaning up, and favoriting user digests."""

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import String, cast, delete, func, or_, select
from sqlalchemy.orm import Session

from app.models.content_cluster import ClusterItem, ContentCluster
from app.models.digest import Digest, DigestItem
from app.models.digest_setting import UserDigestSetting
from app.models.enums import DigestType, ProcessingStatus
from app.models.feed_item import FeedItem
from app.models.source import Source
from app.models.user_item_score import UserItemScore
from app.models.user_source import UserSource
from app.schemas.digest import (
    DigestCreateRequest,
    DigestDetailResponse,
    DigestListItem,
    DigestSettingsResponse,
    DigestSettingsUpdateRequest,
)
from app.schemas.feed import FeedItemListItem, SourceBrief
from app.services.ranking import calculate_card_rank
from app.services.users import require_user

logger = logging.getLogger(__name__)


class DigestService:
    def __init__(self, db: Session):
        self.db = db

    def generate_digest(self, request: DigestCreateRequest) -> DigestDetailResponse:
        """
        Генерация дайджеста по запросу пользователя:
        1. Очистка старых дайджестов (> 3 дней), которые не в избранном.
        2. Определение временного окна: [now - period_hours, now].
        3. Поиск статей из активных источников пользователя.
        4. Исключение дублей: если новость уже попадала в предыдущий дайджест этого пользователя,
           она включается СНОВА ТОЛЬКО если у неё updated_at > даты включения в прошлый дайджест.
        5. Группировка публикаций одного кластера в единую историю (одна карточка на инфоповод).
        6. Сортировка по важности (importance_score DESC, published_at DESC).
        7. Формирование саммари-сводки.
        8. Сохранение дайджеста и связей в БД.
        """
        require_user(self.db, request.user_id)
        # Автоматическая очистка просроченных (> 3 дней не избранных)
        self.cleanup_expired_digests(days=3)

        settings = self.get_or_create_settings(request.user_id)
        effective_period_hours = (
            request.period_hours if request.period_hours is not None else settings.period_hours
        )
        effective_max_items = (
            request.max_items if request.max_items is not None else settings.max_items
        )

        now = datetime.now(UTC).replace(tzinfo=None)
        period_start = now - timedelta(hours=effective_period_hours)
        period_end = now

        # Базовый запрос публикаций пользователя за период с информацией о кластерах
        base_query = (
            select(
                FeedItem,
                Source,
                ClusterItem.cluster_id,
                ClusterItem.is_primary,
                ContentCluster.title.label("cluster_title"),
                ContentCluster.summary.label("cluster_summary"),
                ContentCluster.importance_score.label("cluster_importance_score"),
            )
            .join(Source, FeedItem.source_id == Source.id)
            .join(
                UserSource,
                (UserSource.source_id == Source.id)
                & (UserSource.user_id == request.user_id)
                & (UserSource.enabled.is_(True)),
            )
            .outerjoin(ClusterItem, ClusterItem.feed_item_id == FeedItem.id)
            .outerjoin(ContentCluster, ContentCluster.id == ClusterItem.cluster_id)
            .where(
                FeedItem.processing_status.in_(
                    (ProcessingStatus.NORMALIZED, ProcessingStatus.PROCESSED)
                ),
                or_(
                    (FeedItem.published_at >= period_start) & (FeedItem.published_at <= period_end),
                    (FeedItem.collected_at >= period_start) & (FeedItem.collected_at <= period_end),
                ),
            )
        )

        # Тематическая фильтрация
        if request.type == DigestType.TOPIC and request.topic and request.topic.strip():
            clean_topic = request.topic.strip()
            pattern = f"%{clean_topic}%"
            raw_payload_text = cast(FeedItem.raw_payload, String)
            topic_filter = or_(
                FeedItem.title.ilike(pattern),
                FeedItem.summary.ilike(pattern),
                FeedItem.body_text.ilike(pattern),
                raw_payload_text.ilike(pattern),
                ContentCluster.title.ilike(pattern),
                ContentCluster.summary.ilike(pattern),
            )
            base_query = base_query.where(topic_filter)

        # Сортировка: важность (importance_score), затем дата публикации
        base_query = base_query.order_by(
            func.coalesce(ContentCluster.importance_score, FeedItem.importance_score)
            .desc()
            .nulls_last(),
            FeedItem.published_at.desc().nulls_last(),
            FeedItem.id.desc(),
        )

        candidates = self.db.execute(base_query).all()

        # Проверка дедупликации: исключаем то, что уже было в прошлых дайджестах этого пользователя,
        # ЕСЛИ только пост не обновился (updated_at > included_updated_at)
        candidate_ids = [row[0].id for row in candidates]
        previously_included: dict[int, datetime | None] = {}
        if candidate_ids:
            past_items_query = (
                select(DigestItem.feed_item_id, func.max(DigestItem.included_updated_at))
                .join(Digest, DigestItem.digest_id == Digest.id)
                .where(
                    Digest.user_id == request.user_id,
                    DigestItem.feed_item_id.in_(candidate_ids),
                )
                .group_by(DigestItem.feed_item_id)
            )
            for feed_id, last_update in self.db.execute(past_items_query).all():
                previously_included[feed_id] = last_update

        # Группировка в карточки историй с дедупликацией
        story_cards: list[dict] = []
        cluster_map: dict[int, int] = {}

        for row in candidates:
            (
                feed_item,
                source,
                cluster_id,
                is_primary,
                cluster_title,
                cluster_summary,
                cluster_importance,
            ) = row

            if cluster_id is not None:
                if cluster_id in cluster_map:
                    card = story_cards[cluster_map[cluster_id]]
                    card["feed_items"].append((feed_item, source, bool(is_primary)))
                    if is_primary:
                        card["primary_item"] = feed_item
                        card["primary_source"] = source
                else:
                    cluster_map[cluster_id] = len(story_cards)
                    story_cards.append(
                        {
                            "is_cluster": True,
                            "cluster_id": cluster_id,
                            "cluster_title": cluster_title,
                            "cluster_summary": cluster_summary,
                            "importance_score": cluster_importance or feed_item.importance_score,
                            "primary_item": feed_item,
                            "primary_source": source,
                            "feed_items": [(feed_item, source, bool(is_primary))],
                        }
                    )
            else:
                story_cards.append(
                    {
                        "is_cluster": False,
                        "cluster_id": None,
                        "cluster_title": None,
                        "cluster_summary": None,
                        "importance_score": feed_item.importance_score,
                        "primary_item": feed_item,
                        "primary_source": source,
                        "feed_items": [(feed_item, source, False)],
                    }
                )

        # A new member makes the matching cluster card eligible again. Keep already
        # seen members that also passed the period, topic and subscription filters.
        def is_new_or_updated(item: FeedItem) -> bool:
            if item.id not in previously_included:
                return True
            last_seen_update = previously_included[item.id]
            return bool(
                item.updated_at and (last_seen_update is None or item.updated_at > last_seen_update)
            )

        story_cards = [
            card
            for card in story_cards
            if any(is_new_or_updated(item) for item, _, _ in card["feed_items"])
        ]

        # Load user_item_scores for candidates
        candidate_feed_ids = [row[0].id for row in candidates]
        candidate_cluster_ids = [row[2] for row in candidates if row[2] is not None]

        user_scores_by_feed: dict[int, float] = {}
        user_scores_by_cluster: dict[int, float] = {}

        if candidate_feed_ids or candidate_cluster_ids:
            score_filters = []
            if candidate_feed_ids:
                score_filters.append(UserItemScore.feed_item_id.in_(candidate_feed_ids))
            if candidate_cluster_ids:
                score_filters.append(UserItemScore.cluster_id.in_(candidate_cluster_ids))

            scores_query = select(UserItemScore).where(
                UserItemScore.user_id == request.user_id,
                or_(*score_filters),
            )
            for sc in self.db.execute(scores_query).scalars().all():
                if sc.feed_item_id and sc.relevance_score is not None:
                    user_scores_by_feed[sc.feed_item_id] = sc.relevance_score
                if sc.cluster_id and sc.relevance_score is not None:
                    user_scores_by_cluster[sc.cluster_id] = sc.relevance_score

        # Rank all eligible story cards
        for card in story_cards:
            if card["is_cluster"]:
                for f_item, _src, is_prim in card["feed_items"]:
                    if is_prim:
                        card["primary_item"] = f_item
                        card["primary_source"] = _src
                        break

            rank = calculate_card_rank(
                is_cluster=card["is_cluster"],
                cluster_id=card["cluster_id"],
                primary_item=card["primary_item"],
                feed_items=card["feed_items"],
                cluster_importance=card.get("importance_score"),
                user_scores_by_feed=user_scores_by_feed,
                user_scores_by_cluster=user_scores_by_cluster,
                now=now,
            )
            card["rank_score"] = rank

        # Sort by rank_score DESC, importance_score DESC, published_at DESC, id DESC
        def _digest_card_sort_key(c: dict) -> tuple:
            p_item = c["primary_item"]
            pub = p_item.published_at or p_item.collected_at or datetime.min
            imp = c.get("importance_score") or getattr(p_item, "importance_score", None) or 0.0
            return (c["rank_score"], imp, pub, p_item.id)

        story_cards.sort(key=_digest_card_sort_key, reverse=True)

        # Ограничиваем топ материалов для одного дайджеста согласно настройкам (по умолчанию 15)
        story_cards = story_cards[:effective_max_items]

        # Преобразуем отобранные истории в FeedItemListItem
        selected_card_items: list[FeedItemListItem] = []
        all_included_feed_items: list[FeedItem] = []

        for card in story_cards:
            primary_item: FeedItem = card["primary_item"]
            primary_source: Source = card["primary_source"]
            if card["is_cluster"]:
                for f_item, src, is_prim in card["feed_items"]:
                    if is_prim:
                        primary_item = f_item
                        primary_source = src
                        break

            # Title
            if card["is_cluster"] and card["cluster_title"]:
                title_text = card["cluster_title"].strip()
            else:
                title_text = (primary_item.title or "Без названия").strip()

            # Summary
            if card["is_cluster"] and card["cluster_summary"]:
                summary_text = card["cluster_summary"].strip()
            else:
                summary_text = primary_item.summary
                if not summary_text:
                    raw_body = (primary_item.body_text or "").strip()
                    summary_text = (
                        raw_body[:250].rstrip() + "..." if len(raw_body) > 250 else raw_body
                    )

            # Related sources
            seen_source_ids = set()
            related_sources: list[SourceBrief] = []
            for f_item, src, _ in card["feed_items"]:
                all_included_feed_items.append(f_item)
                if src.id not in seen_source_ids:
                    seen_source_ids.add(src.id)
                    related_sources.append(
                        SourceBrief(
                            id=src.id,
                            name=src.name,
                            type=src.type,
                        )
                    )

            selected_card_items.append(
                FeedItemListItem(
                    id=primary_item.id,
                    title=title_text,
                    summary=summary_text,
                    published_at=primary_item.published_at,
                    source=SourceBrief(
                        id=primary_source.id,
                        name=primary_source.name,
                        type=primary_source.type,
                    ),
                    original_url=primary_item.original_url,
                    is_cluster=card["is_cluster"],
                    cluster_id=card["cluster_id"],
                    items_count=len(card["feed_items"]),
                    related_sources=related_sources,
                )
            )

        # Формируем заголовок дайджеста
        time_str = now.strftime("%d.%m.%Y %H:%M")
        if request.type == DigestType.TOPIC and request.topic:
            title = f"Дайджест: {request.topic.strip()} ({time_str})"
        else:
            title = f"Общий дайджест за {time_str}"

        # Генерация сводки
        summary_text = self._build_digest_summary_from_cards(
            title=title,
            cards=story_cards,
            topic=request.topic,
        )

        # Сохранение в БД
        digest = Digest(
            user_id=request.user_id,
            type=request.type,
            title=title,
            topic=request.topic.strip() if request.topic else None,
            summary_text=summary_text,
            period_start=period_start,
            period_end=period_end,
            items_count=len(selected_card_items),
            items_snapshot=[item.model_dump(mode="json") for item in selected_card_items],
            is_favorite=False,
            created_at=now,
        )
        self.db.add(digest)
        self.db.flush()

        # Добавляем ссылки на вошедшие элементы
        for feed_item in all_included_feed_items:
            self.db.add(
                DigestItem(
                    digest_id=digest.id,
                    feed_item_id=feed_item.id,
                    included_updated_at=feed_item.updated_at,
                )
            )

        self.db.commit()

        # Формируем ответ
        return DigestDetailResponse(
            id=digest.id,
            user_id=digest.user_id,
            type=digest.type,
            title=digest.title,
            topic=digest.topic,
            summary_text=digest.summary_text,
            period_start=digest.period_start,
            period_end=digest.period_end,
            items_count=digest.items_count,
            is_favorite=digest.is_favorite,
            created_at=digest.created_at,
            items=selected_card_items,
        )

    def list_user_digests(
        self,
        user_id: int,
        digest_type: DigestType | None = None,
        only_favorites: bool = False,
    ) -> list[DigestListItem]:
        """Получить список карточек/плашек дайджестов пользователя."""
        require_user(self.db, user_id)
        query = select(Digest).where(Digest.user_id == user_id)

        if digest_type:
            query = query.where(Digest.type == digest_type)
        if only_favorites:
            query = query.where(Digest.is_favorite.is_(True))

        query = query.order_by(Digest.created_at.desc(), Digest.id.desc())
        digests = self.db.scalars(query).all()

        results: list[DigestListItem] = []
        for d in digests:
            preview = (d.summary_text or "").strip()
            if len(preview) > 200:
                preview = preview[:200].rstrip() + "..."

            results.append(
                DigestListItem(
                    id=d.id,
                    user_id=d.user_id,
                    type=d.type,
                    title=d.title,
                    topic=d.topic,
                    preview=preview,
                    period_start=d.period_start,
                    period_end=d.period_end,
                    items_count=d.items_count,
                    is_favorite=d.is_favorite,
                    created_at=d.created_at,
                )
            )
        return results

    def get_digest_detail(self, user_id: int, digest_id: int) -> DigestDetailResponse | None:
        """Получить только собственный дайджест пользователя."""
        require_user(self.db, user_id)
        digest = self.db.get(Digest, digest_id)
        if digest is None or digest.user_id != user_id:
            return None

        return DigestDetailResponse(
            id=digest.id,
            user_id=digest.user_id,
            type=digest.type,
            title=digest.title,
            topic=digest.topic,
            summary_text=digest.summary_text,
            period_start=digest.period_start,
            period_end=digest.period_end,
            items_count=digest.items_count,
            is_favorite=digest.is_favorite,
            created_at=digest.created_at,
            items=[FeedItemListItem.model_validate(item) for item in digest.items_snapshot],
        )

    def toggle_favorite(self, user_id: int, digest_id: int) -> bool | None:
        """Переключить флаг избранного у дайджеста. Избранные не удаляются через 3 дня."""
        require_user(self.db, user_id)
        digest = self.db.get(Digest, digest_id)
        if digest is None or digest.user_id != user_id:
            return None
        digest.is_favorite = not digest.is_favorite
        self.db.commit()
        return digest.is_favorite

    def cleanup_expired_digests(self, days: int = 3) -> int:
        """Удаляет дайджесты старше указанного количества дней (кроме сохранённых в избранное)."""
        cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=days)
        statement = delete(Digest).where(
            Digest.created_at < cutoff,
            Digest.is_favorite.is_(False),
        )
        result = self.db.execute(statement)
        self.db.commit()
        return result.rowcount or 0

    def _build_digest_summary_from_cards(
        self,
        title: str,
        cards: list[dict],
        topic: str | None,
    ) -> str:
        """Сборка структурированного текста дайджеста из сгруппированных историй/кластеров."""
        if not cards:
            topic_hint = f" по теме «{topic}»" if topic else ""
            return (
                f"{title}\n\nЗа прошедший период новых значимых публикаций{topic_hint} "
                "в ваших источниках не обнаружено."
            )

        lines = [
            f"### {title}",
            f"**Отобрано ключевых инфоповодов:** {len(cards)}\n",
            "#### Главные события и темы:",
        ]

        for idx, card in enumerate(cards, 1):
            primary_item: FeedItem = card["primary_item"]
            if card["is_cluster"]:
                for f_item, _, is_prim in card["feed_items"]:
                    if is_prim:
                        primary_item = f_item
                        break

            if card["is_cluster"] and card["cluster_title"]:
                post_title = card["cluster_title"].strip()
            else:
                post_title = (primary_item.title or "Без названия").strip()

            if card["is_cluster"] and card["cluster_summary"]:
                summary = card["cluster_summary"].strip()
            else:
                raw_summary = primary_item.summary or (primary_item.body_text[:180] + "...")
                summary = raw_summary.strip()

            score_val = card.get("importance_score") or primary_item.importance_score
            score_badge = f" [Важность: {score_val:.1f}]" if score_val is not None else ""

            # Источники
            sources_names = []
            seen = set()
            for _, s, _ in card["feed_items"]:
                if s.name not in seen:
                    seen.add(s.name)
                    sources_names.append(s.name)
            sources_str = ", ".join(sources_names)

            cluster_badge = f" ({sources_str})"
            lines.append(
                f"{idx}. **{post_title}**{cluster_badge}{score_badge}\n"
                f"   {summary}\n   [Читать далее]({primary_item.original_url})"
            )

        return "\n\n".join(lines)

    def get_or_create_settings(self, user_id: int) -> UserDigestSetting:
        """Получить текущие настройки дайджестов пользователя или создать с дефолтными значениями."""
        require_user(self.db, user_id)
        settings = self.db.get(UserDigestSetting, user_id)
        if not settings:
            settings = UserDigestSetting(user_id=user_id, period_hours=24, max_items=15)
            self.db.add(settings)
            self.db.commit()
            self.db.refresh(settings)
        return settings

    def get_settings(self, user_id: int) -> DigestSettingsResponse:
        """Получить настройки дайджестов пользователя в виде схемы ответа."""
        settings = self.get_or_create_settings(user_id)
        return DigestSettingsResponse(
            user_id=settings.user_id,
            period_hours=settings.period_hours,
            max_items=settings.max_items,
            updated_at=settings.updated_at,
        )

    def update_settings(
        self, user_id: int, request: DigestSettingsUpdateRequest
    ) -> DigestSettingsResponse:
        """Обновить настройки генерации дайджестов пользователя."""
        settings = self.get_or_create_settings(user_id)
        changed = False

        if request.period_hours is not None:
            settings.period_hours = request.period_hours
            changed = True
        if request.max_items is not None:
            settings.max_items = request.max_items
            changed = True

        if changed:
            settings.updated_at = datetime.now(UTC).replace(tzinfo=None)
            self.db.add(settings)
            self.db.commit()
            self.db.refresh(settings)

        return DigestSettingsResponse(
            user_id=settings.user_id,
            period_hours=settings.period_hours,
            max_items=settings.max_items,
            updated_at=settings.updated_at,
        )

