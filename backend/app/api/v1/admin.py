"""Административные эндпоинты. Доступ — только роль admin."""

import logging

from fastapi import APIRouter, Depends, Request
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, require_roles
from app.api.errors import ApiError
from app.config import get_settings
from app.db import get_session
from app.models.identity import User
from app.models.subscriptions import Subscription
from app.schemas.auth import SyncResult
from app.schemas.subscriptions import AdminSubscriptionCreate, SubscriptionResponse
from app.services.audit import write_audit
from app.services.login import sync_directory
from app.services.subscriptions import (
    active_topic,
    get_subscription,
    validate_channels,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])


@router.post(
    "/ldap/sync",
    response_model=SyncResult,
    summary="Внеочередная синхронизация ролей из каталога",
)
async def ldap_sync(
    request: Request,
    admin: CurrentUser = Depends(require_roles("admin")),
    session: AsyncSession = Depends(get_session),
) -> SyncResult:
    stats = await sync_directory(
        session,
        get_settings(),
        actor_id=admin.id,
        request_id=getattr(request.state, "request_id", None),
    )
    await session.commit()
    logger.info("ldap sync finished", extra=dict(stats))
    return SyncResult(**stats)


@router.post(
    "/subscriptions",
    response_model=SubscriptionResponse,
    status_code=201,
    summary="Назначить тему пользователю",
)
async def assign_subscription(
    payload: AdminSubscriptionCreate,
    request: Request,
    admin: CurrentUser = Depends(require_roles("admin")),
    session: AsyncSession = Depends(get_session),
) -> SubscriptionResponse:
    """Назначенная подписка (origin=assigned) снимается только администратором."""
    topic = await active_topic(session, payload.topic_id)
    user = await session.get(User, payload.user_id)
    if user is None:
        raise ApiError(404, "USER_NOT_FOUND", "Пользователь не найден")

    existing = (
        await session.execute(
            select(Subscription).where(
                Subscription.user_id == payload.user_id,
                Subscription.topic_id == payload.topic_id,
            )
        )
    ).scalar_one_or_none()

    if existing is None:
        subscription = Subscription(
            user_id=payload.user_id,
            topic_id=payload.topic_id,
            origin="assigned",
            channels=validate_channels(payload.channels),
            granted_by=admin.id,
        )
        session.add(subscription)
    else:
        # Добровольная подписка на ту же тему превращается в назначенную
        existing.origin = "assigned"
        existing.granted_by = admin.id
        existing.channels = validate_channels(payload.channels)
        subscription = existing
    await session.flush()

    await write_audit(
        session,
        action="subscription.assign",
        entity_type="subscription",
        entity_id=str(subscription.id),
        actor_id=admin.id,
        new_value={"user_id": payload.user_id, "topic_id": payload.topic_id},
        request_id=getattr(request.state, "request_id", None),
    )
    await session.commit()

    return SubscriptionResponse(
        topic_id=topic.id,
        topic_code=topic.code,
        topic_name=topic.name,
        topic_description=topic.description,
        origin=subscription.origin,
        channels=list(subscription.channels),
        muted_until=subscription.muted_until,
        is_mandatory=True,
        subscription_id=subscription.id,
        can_unsubscribe=False,
    )


@router.delete(
    "/subscriptions/{subscription_id}",
    status_code=204,
    summary="Снять назначенную подписку",
)
async def revoke_subscription(
    subscription_id: int,
    request: Request,
    admin: CurrentUser = Depends(require_roles("admin")),
    session: AsyncSession = Depends(get_session),
) -> None:
    subscription = await get_subscription(session, subscription_id)
    await session.execute(delete(Subscription).where(Subscription.id == subscription_id))
    await write_audit(
        session,
        action="subscription.revoke",
        entity_type="subscription",
        entity_id=str(subscription_id),
        actor_id=admin.id,
        old_value={
            "user_id": subscription.user_id,
            "topic_id": subscription.topic_id,
            "origin": subscription.origin,
        },
        request_id=getattr(request.state, "request_id", None),
    )
    await session.commit()
