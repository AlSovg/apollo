"""Подписки пользователя.

Экран один, вкладки две: «Обязательные» и «Доступные». Фильтров по severity,
тегам и объектам здесь нет осознанно — вся сложность отбора живёт в правиле
темы, которое настраивает администратор.
"""

import logging

from fastapi import APIRouter, Depends, Request
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, current_user
from app.api.errors import ApiError
from app.config import get_settings
from app.db import get_session
from app.models.subscriptions import Subscription
from app.schemas.subscriptions import (
    SubscriptionCreate,
    SubscriptionResponse,
    SubscriptionsResponse,
    SubscriptionUpdate,
)
from app.services.audit import write_audit
from app.services.subscriptions import (
    EffectiveSubscription,
    cap_mute,
    get_effective_subscriptions,
    get_subscription,
    self_subscribe,
    self_unsubscribe,
    validate_channels,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/subscriptions", tags=["subscriptions"])


def _to_response(row: EffectiveSubscription) -> SubscriptionResponse:
    return SubscriptionResponse(
        topic_id=row.topic_id,
        topic_code=row.topic_code,
        topic_name=row.topic_name,
        topic_description=row.topic_description,
        origin=row.origin,
        channels=row.channels,
        muted_until=row.muted_until,
        is_mandatory=row.is_mandatory,
        subscription_id=row.subscription_id,
        can_unsubscribe=row.origin == "self",
    )


@router.get("", response_model=SubscriptionsResponse, summary="Мои подписки")
async def my_subscriptions(
    user: CurrentUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> SubscriptionsResponse:
    rows = [_to_response(row) for row in await get_effective_subscriptions(session, user.id)]
    return SubscriptionsResponse(
        mandatory=[row for row in rows if row.is_mandatory],
        voluntary=[row for row in rows if not row.is_mandatory],
    )


@router.post("", response_model=SubscriptionResponse, status_code=201, summary="Подписаться")
async def subscribe(
    payload: SubscriptionCreate,
    request: Request,
    user: CurrentUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> SubscriptionResponse:
    subscription = await self_subscribe(
        session,
        user_id=user.id,
        topic_id=payload.topic_id,
        channels=validate_channels(payload.channels),
    )
    await write_audit(
        session,
        action="subscription.self.create",
        entity_type="subscription",
        entity_id=str(subscription.id),
        actor_id=user.id,
        new_value={"topic_id": payload.topic_id, "channels": subscription.channels},
        request_id=getattr(request.state, "request_id", None),
    )
    await session.commit()

    return _effective_or_404(await get_effective_subscriptions(session, user.id), payload.topic_id)


@router.patch(
    "/{subscription_id}", response_model=SubscriptionResponse, summary="Каналы и глушение"
)
async def update_subscription(
    subscription_id: int,
    payload: SubscriptionUpdate,
    request: Request,
    user: CurrentUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> SubscriptionResponse:
    subscription = await get_subscription(session, subscription_id)
    if subscription.user_id != user.id:
        raise ApiError(404, "SUBSCRIPTION_NOT_FOUND", "Подписка не найдена")

    values: dict[str, object] = {}
    if payload.channels is not None:
        values["channels"] = validate_channels(payload.channels)
    if "muted_until" in payload.model_fields_set:
        # Глушение ограничено сверху: подписка не должна замолкать навсегда
        values["muted_until"] = cap_mute(
            payload.muted_until, max_hours=get_settings().max_mute_hours
        )

    if values:
        await session.execute(
            update(Subscription).where(Subscription.id == subscription_id).values(**values)
        )
        await write_audit(
            session,
            action="subscription.update",
            entity_type="subscription",
            entity_id=str(subscription_id),
            actor_id=user.id,
            new_value={key: str(value) for key, value in values.items()},
            request_id=getattr(request.state, "request_id", None),
        )
        await session.commit()

    return _effective_or_404(
        await get_effective_subscriptions(session, user.id), subscription.topic_id
    )


@router.delete("/{subscription_id}", status_code=204, summary="Отписаться")
async def unsubscribe(
    subscription_id: int,
    request: Request,
    user: CurrentUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    """Только добровольная подписка: назначенную снимает администратор."""
    subscription = await self_unsubscribe(
        session, user_id=user.id, subscription_id=subscription_id
    )
    await write_audit(
        session,
        action="subscription.self.delete",
        entity_type="subscription",
        entity_id=str(subscription_id),
        actor_id=user.id,
        old_value={"topic_id": subscription.topic_id},
        request_id=getattr(request.state, "request_id", None),
    )
    await session.commit()


def _effective_or_404(
    rows: list[EffectiveSubscription], topic_id: int
) -> SubscriptionResponse:
    for row in rows:
        if row.topic_id == topic_id:
            return _to_response(row)
    # Тема деактивирована или пользователь отключён — подписка недействительна
    raise ApiError(404, "SUBSCRIPTION_NOT_FOUND", "Подписка не найдена")
