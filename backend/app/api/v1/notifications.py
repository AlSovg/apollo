"""Веб-инбокс дежурного — основной канал доставки.

Читает вьюху v_user_inbox: там уже собран контекст инцидента и сводка модели.
Пользователь видит только свои уведомления.
"""

import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, current_user
from app.api.errors import ApiError
from app.db import get_session
from app.models.delivery import Notification
from app.schemas.notifications import (
    DeliveryAttempt,
    InboxItem,
    InboxPage,
    ReadResult,
    UnreadCount,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/notifications", tags=["notifications"])

_INBOX_QUERY = """
    SELECT notification_id, incident_id, severity, rule_name, object_name,
           topic_name, summary, incident_status, created_at, read_at
    FROM v_user_inbox
    WHERE user_id = :user_id
    {filters}
    ORDER BY read_at NULLS FIRST, created_at DESC
    LIMIT :limit OFFSET :offset
"""

_COUNT_QUERY = """
    SELECT count(*) FROM v_user_inbox WHERE user_id = :user_id {filters}
"""

_SEVERITY_COUNT_QUERY = """
    SELECT severity, count(*) FROM v_user_inbox
    WHERE user_id = :user_id {filters}
    GROUP BY severity
"""


@router.get("", response_model=InboxPage, summary="Инбокс")
async def inbox(
    unread_only: bool = False,
    severity: int | None = Query(default=None, ge=0, le=5),
    severity_min: int | None = Query(default=None, ge=0, le=5),
    status: str | None = Query(default=None, pattern="^(open|resolved)$"),
    search: str | None = Query(default=None, max_length=128),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=100),
    user: CurrentUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> InboxPage:
    params: dict[str, object] = {"user_id": user.id}
    # Условия, общие для списка и для счётчиков по уровням
    common: list[str] = []
    if unread_only:
        common.append("AND read_at IS NULL")
    if status is not None:
        common.append("AND incident_status = :status")
        params["status"] = status
    if search:
        # Дежурный ищет по тому, что видит: правилу или объекту
        common.append("AND (rule_name ILIKE :pattern OR object_name ILIKE :pattern)")
        params["pattern"] = f"%{search.strip()}%"

    # Отбор по критичности в счётчики не входит: выбор уровня не должен обнулять
    # остальные плитки, иначе фильтр слепнет после первого же клика.
    # `severity` — ровно этот уровень (число на плитке обязано совпадать с
    # числом строк), `severity_min` — уровень и выше.
    listing = list(common)
    if severity is not None:
        listing.append("AND severity = :severity")
        params["severity"] = severity
    if severity_min is not None:
        listing.append("AND severity >= :severity_min")
        params["severity_min"] = severity_min

    rows = await session.execute(
        text(_INBOX_QUERY.format(filters="\n".join(listing))),
        {**params, "limit": size, "offset": (page - 1) * size},
    )
    total = await session.scalar(
        text(_COUNT_QUERY.format(filters="\n".join(listing))), params
    )
    counts = {level: 0 for level in range(6)}
    count_rows = await session.execute(
        text(_SEVERITY_COUNT_QUERY.format(filters="\n".join(common))),
        {
            key: value
            for key, value in params.items()
            if key not in ("severity", "severity_min")
        },
    )
    for level, count in count_rows.all():
        counts[level] = count

    return InboxPage(
        severity_counts=counts,
        items=[
            InboxItem(
                notification_id=row.notification_id,
                incident_id=row.incident_id,
                severity=row.severity,
                rule_name=row.rule_name,
                object_name=row.object_name,
                topic_name=row.topic_name,
                summary=row.summary,
                incident_status=row.incident_status,
                created_at=row.created_at,
                read_at=row.read_at,
            )
            for row in rows
        ],
        total=int(total or 0),
        page=page,
        size=size,
    )


@router.get("/unread-count", response_model=UnreadCount, summary="Непрочитанные")
async def unread_count(
    user: CurrentUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> UnreadCount:
    count = await session.scalar(
        select(func.count())
        .select_from(Notification)
        .where(
            Notification.user_id == user.id,
            Notification.channel == "web",
            Notification.status == "sent",
            Notification.read_at.is_(None),
        )
    )
    return UnreadCount(unread=int(count or 0))


@router.post("/{notification_id}/read", response_model=ReadResult, summary="Прочитано")
async def mark_read(
    notification_id: int,
    user: CurrentUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> ReadResult:
    notification = await session.get(Notification, notification_id)
    if notification is None or notification.user_id != user.id:
        raise ApiError(404, "NOTIFICATION_NOT_FOUND", "Уведомление не найдено")

    if notification.read_at is None:
        await session.execute(
            update(Notification)
            .where(Notification.id == notification_id)
            .values(read_at=datetime.now(tz=UTC))
        )
        await session.commit()
        return ReadResult(updated=1)
    return ReadResult(updated=0)


@router.post("/read-all", response_model=ReadResult, summary="Прочитать всё")
async def mark_all_read(
    user: CurrentUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> ReadResult:
    unread = (
        await session.execute(
            select(Notification.id).where(
                Notification.user_id == user.id,
                Notification.channel == "web",
                Notification.read_at.is_(None),
            )
        )
    ).scalars().all()
    if not unread:
        return ReadResult(updated=0)

    await session.execute(
        update(Notification)
        .where(Notification.id.in_(unread))
        .values(read_at=datetime.now(tz=UTC))
    )
    await session.commit()
    return ReadResult(updated=len(unread))


@router.get(
    "/{notification_id}/delivery",
    response_model=list[DeliveryAttempt],
    summary="История доставки по инциденту",
)
async def delivery_history(
    notification_id: int,
    user: CurrentUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> list[DeliveryAttempt]:
    """Все каналы, по которым инцидент доставлялся этому пользователю."""
    notification = await session.get(Notification, notification_id)
    if notification is None or notification.user_id != user.id:
        raise ApiError(404, "NOTIFICATION_NOT_FOUND", "Уведомление не найдено")

    rows = (
        await session.execute(
            select(Notification)
            .where(
                Notification.incident_id == notification.incident_id,
                Notification.user_id == user.id,
            )
            .order_by(Notification.channel)
        )
    ).scalars().all()

    return [
        DeliveryAttempt(
            notification_id=row.id,
            channel=row.channel,
            status=row.status,
            attempts=row.attempts,
            last_error=row.last_error,
            next_retry_at=row.next_retry_at,
            created_at=row.created_at,
            sent_at=row.sent_at,
        )
        for row in rows
    ]
