"""Service for generating, retrieving, cleaning up, and favoriting user digests."""

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import String, cast, delete, func, or_, select
from sqlalchemy.orm import Session

from app.models.digest import Digest, DigestItem
from app.models.enums import DigestType
from app.models.feed_item import FeedItem
from app.models.source import Source
from app.models.user_source import UserSource
from app.schemas.digest import (
    DigestCreateRequest,
    DigestDetailResponse,
    DigestListItem,
)
from app.schemas.feed import FeedItemListItem, SourceBrief

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
        5. Сортировка по важности (importance_score DESC, published_at DESC).
        6. Формирование саммари-сводки.
        7. Сохранение дайджеста и связей в БД.
        """
        # Автоматическая очистка просроченных (> 3 дней не избранных)
        self.cleanup_expired_digests(days=3)

        now = datetime.now(UTC).replace(tzinfo=None)
        period_start = now - timedelta(hours=request.period_hours)
        period_end = now

        # Базовый запрос публикаций пользователя за период
        base_query = (
            select(FeedItem, Source)
            .join(Source, FeedItem.source_id == Source.id)
            .join(
                UserSource,
                (UserSource.source_id == Source.id)
                & (UserSource.user_id == request.user_id)
                & (UserSource.enabled.is_(True)),
            )
            .where(
                or_(
                    (FeedItem.published_at >= period_start) & (FeedItem.published_at <= period_end),
                    (FeedItem.collected_at >= period_start) & (FeedItem.collected_at <= period_end),
                )
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
            )
            base_query = base_query.where(topic_filter)

        # Сортировка: важность (importance_score), затем дата публикации
        base_query = base_query.order_by(
            FeedItem.importance_score.desc().nulls_last(),
            FeedItem.published_at.desc().nulls_last(),
            FeedItem.id.desc(),
        )

        candidates = self.db.execute(base_query).all()

        # Проверка дедупликации: исключаем то, что уже было в прошлых дайджестах этого пользователя,
        # ЕСЛИ только пост не обновился (updated_at > included_updated_at)
        candidate_ids = [item.id for item, _ in candidates]
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

        selected_items: list[tuple[FeedItem, Source]] = []
        for feed_item, source in candidates:
            if feed_item.id in previously_included:
                last_seen_update = previously_included[feed_item.id]
                # Пропускаем пост без updated_at или без обновлений после прошлого дайджеста.
                if not feed_item.updated_at:
                    continue
                if last_seen_update and feed_item.updated_at <= last_seen_update:
                    continue
            selected_items.append((feed_item, source))

        # Ограничиваем топ материалов для одного дайджеста (например, топ-15 наиболее важных)
        selected_items = selected_items[:15]

        # Формируем заголовок дайджеста
        time_str = now.strftime("%d.%m.%Y %H:%M")
        if request.type == DigestType.TOPIC and request.topic:
            title = f"Дайджест: {request.topic.strip()} ({time_str})"
        else:
            title = f"Общий дайджест за {time_str}"

        # Генерация сводки
        summary_text = self._build_digest_summary(
            title=title,
            items=selected_items,
            digest_type=request.type,
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
            items_count=len(selected_items),
            is_favorite=False,
            created_at=now,
        )
        self.db.add(digest)
        self.db.flush()  # Получаем digest.id

        # Добавляем ссылки на вошедшие элементы
        for feed_item, _ in selected_items:
            self.db.add(
                DigestItem(
                    digest_id=digest.id,
                    feed_item_id=feed_item.id,
                    included_updated_at=feed_item.updated_at,
                )
            )

        self.db.commit()

        # Формируем ответ
        return self._to_detail_response(digest, selected_items)

    def list_user_digests(
        self,
        user_id: int,
        digest_type: DigestType | None = None,
        only_favorites: bool = False,
    ) -> list[DigestListItem]:
        """Получить список карточек/плашек дайджестов пользователя."""
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

    def get_digest_detail(self, digest_id: int) -> DigestDetailResponse | None:
        """Получить развернутый дайджест по его ID."""
        digest = self.db.get(Digest, digest_id)
        if not digest:
            return None

        # Получаем вошедшие посты
        items_query = (
            select(FeedItem, Source)
            .join(DigestItem, DigestItem.feed_item_id == FeedItem.id)
            .join(Source, FeedItem.source_id == Source.id)
            .where(DigestItem.digest_id == digest.id)
            .order_by(
                FeedItem.importance_score.desc().nulls_last(),
                FeedItem.published_at.desc().nulls_last(),
            )
        )
        items = self.db.execute(items_query).all()
        return self._to_detail_response(digest, items)

    def toggle_favorite(self, digest_id: int) -> bool | None:
        """Переключить флаг избранного у дайджеста. Избранные не удаляются через 3 дня."""
        digest = self.db.get(Digest, digest_id)
        if not digest:
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

    def _build_digest_summary(
        self,
        title: str,
        items: list[tuple[FeedItem, Source]],
        digest_type: DigestType,
        topic: str | None,
    ) -> str:
        """Сборка структурированного текста дайджеста из отобранных материалов."""
        if not items:
            topic_hint = f" по теме «{topic}»" if topic else ""
            return (
                f"{title}\n\nЗа прошедший период новых значимых публикаций{topic_hint} "
                "в ваших источниках не обнаружено."
            )

        lines = [
            f"### {title}",
            f"**Отобрано ключевых материалов:** {len(items)}\n",
            "#### Главные события и темы:",
        ]

        for idx, (feed_item, source) in enumerate(items, 1):
            post_title = (feed_item.title or "Без названия").strip()
            summary = (feed_item.summary or feed_item.body_text[:180] + "...").strip()
            score_badge = (
                f" [Важность: {feed_item.importance_score:.1f}]"
                if feed_item.importance_score is not None
                else ""
            )
            lines.append(
                f"{idx}. **{post_title}** ({source.name}){score_badge}\n"
                f"   {summary}\n   [Читать далее]({feed_item.original_url})"
            )

        return "\n\n".join(lines)

    def _to_detail_response(
        self, digest: Digest, items: list[tuple[FeedItem, Source]]
    ) -> DigestDetailResponse:
        """Преобразование модели Digest и связанных публикаций в DTO ответа."""
        feed_items_list: list[FeedItemListItem] = []
        for feed_item, source in items:
            title_text = (feed_item.title or "БЕЗ НАЗВАНИЯ").strip().upper()
            summary_text = feed_item.summary
            if not summary_text:
                raw_body = (feed_item.body_text or "").strip()
                summary_text = raw_body[:250] + "..." if len(raw_body) > 250 else raw_body

            feed_items_list.append(
                FeedItemListItem(
                    id=feed_item.id,
                    title=title_text,
                    summary=summary_text,
                    published_at=feed_item.published_at,
                    source=SourceBrief(
                        id=source.id,
                        name=source.name,
                        type=source.type,
                    ),
                    original_url=feed_item.original_url,
                )
            )

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
            items=feed_items_list,
        )
