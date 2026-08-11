"""Каталог тем. Чтение — всем аутентифицированным, изменение — админу."""

import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Request
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, current_user, require_roles
from app.api.errors import ApiError
from app.db import get_session
from app.models.subscriptions import Topic, TopicRole
from app.schemas.subscriptions import (
    TopicCreate,
    TopicResponse,
    TopicRolesUpdate,
    TopicUpdate,
)
from app.services.audit import write_audit
from app.services.subscriptions import get_effective_subscriptions
from app.services.topics import MatchRule

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/topics", tags=["topics"])


async def _roles_by_topic(session: AsyncSession) -> dict[int, list[str]]:
    rows = (await session.execute(select(TopicRole))).scalars().all()
    mapping: dict[int, list[str]] = {}
    for row in rows:
        mapping.setdefault(row.topic_id, []).append(row.role_code)
    return {topic_id: sorted(roles) for topic_id, roles in mapping.items()}


def _to_response(topic: Topic, roles: list[str], state: str) -> TopicResponse:
    return TopicResponse(
        id=topic.id,
        code=topic.code,
        name=topic.name,
        description=topic.description,
        match_rule=MatchRule.model_validate(topic.match_rule or {}),
        is_active=topic.is_active,
        roles=roles,
        state=state,
    )


async def _get_topic(session: AsyncSession, topic_id: int) -> Topic:
    topic = await session.get(Topic, topic_id)
    if topic is None:
        raise ApiError(404, "TOPIC_NOT_FOUND", "Тема не найдена")
    return topic


@router.get("", response_model=list[TopicResponse], summary="Каталог тем")
async def list_topics(
    include_inactive: bool = False,
    user: CurrentUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> list[TopicResponse]:
    """Каждая тема размечена состоянием для текущего пользователя."""
    query = select(Topic).order_by(Topic.name)
    if not include_inactive:
        query = query.where(Topic.is_active)
    topics = (await session.execute(query)).scalars().all()

    roles = await _roles_by_topic(session)
    effective = {row.topic_id: row for row in await get_effective_subscriptions(session, user.id)}

    result = []
    for topic in topics:
        subscription = effective.get(topic.id)
        if subscription is None:
            state = "available"
        elif subscription.is_mandatory:
            state = "mandatory"
        else:
            state = "subscribed"
        result.append(_to_response(topic, roles.get(topic.id, []), state))
    return result


@router.post("", response_model=TopicResponse, status_code=201, summary="Создать тему")
async def create_topic(
    payload: TopicCreate,
    request: Request,
    admin: CurrentUser = Depends(require_roles("admin")),
    session: AsyncSession = Depends(get_session),
) -> TopicResponse:
    existing = (
        await session.execute(select(Topic).where(Topic.code == payload.code))
    ).scalar_one_or_none()
    if existing is not None:
        raise ApiError(409, "TOPIC_EXISTS", "Тема с таким кодом уже есть")

    topic = Topic(
        code=payload.code,
        name=payload.name,
        description=payload.description,
        match_rule=payload.match_rule.model_dump(exclude_none=True),
        created_by=admin.id,
    )
    session.add(topic)
    await session.flush()

    await write_audit(
        session,
        action="topic.create",
        entity_type="topic",
        entity_id=str(topic.id),
        actor_id=admin.id,
        new_value={"code": topic.code, "match_rule": topic.match_rule},
        request_id=getattr(request.state, "request_id", None),
    )
    await session.commit()
    return _to_response(topic, [], "available")


@router.patch("/{topic_id}", response_model=TopicResponse, summary="Изменить тему")
async def update_topic(
    topic_id: int,
    payload: TopicUpdate,
    request: Request,
    admin: CurrentUser = Depends(require_roles("admin")),
    session: AsyncSession = Depends(get_session),
) -> TopicResponse:
    topic = await _get_topic(session, topic_id)
    before = {
        "name": topic.name,
        "match_rule": topic.match_rule,
        "is_active": topic.is_active,
    }

    values: dict[str, object] = {"updated_at": datetime.now(tz=UTC)}
    if payload.name is not None:
        values["name"] = payload.name
    if payload.description is not None:
        values["description"] = payload.description
    if payload.match_rule is not None:
        values["match_rule"] = payload.match_rule.model_dump(exclude_none=True)
    if payload.is_active is not None:
        values["is_active"] = payload.is_active

    await session.execute(update(Topic).where(Topic.id == topic_id).values(**values))
    await session.refresh(topic)

    await write_audit(
        session,
        action="topic.update",
        entity_type="topic",
        entity_id=str(topic_id),
        actor_id=admin.id,
        old_value=before,
        new_value={
            "name": topic.name,
            "match_rule": topic.match_rule,
            "is_active": topic.is_active,
        },
        request_id=getattr(request.state, "request_id", None),
    )
    await session.commit()

    roles = (await _roles_by_topic(session)).get(topic_id, [])
    return _to_response(topic, roles, "available")


@router.delete("/{topic_id}", status_code=204, summary="Деактивировать тему")
async def deactivate_topic(
    topic_id: int,
    request: Request,
    admin: CurrentUser = Depends(require_roles("admin")),
    session: AsyncSession = Depends(get_session),
) -> None:
    """Мягкое удаление: история подписок и уведомлений остаётся достоверной."""
    topic = await _get_topic(session, topic_id)
    await session.execute(update(Topic).where(Topic.id == topic_id).values(is_active=False))
    await write_audit(
        session,
        action="topic.deactivate",
        entity_type="topic",
        entity_id=str(topic_id),
        actor_id=admin.id,
        old_value={"is_active": topic.is_active},
        new_value={"is_active": False},
        request_id=getattr(request.state, "request_id", None),
    )
    await session.commit()


@router.put(
    "/{topic_id}/roles",
    response_model=TopicResponse,
    summary="Привязка темы к ролям (обязательные подписки)",
)
async def set_topic_roles(
    topic_id: int,
    payload: TopicRolesUpdate,
    request: Request,
    admin: CurrentUser = Depends(require_roles("admin")),
    session: AsyncSession = Depends(get_session),
) -> TopicResponse:
    topic = await _get_topic(session, topic_id)
    before = (await _roles_by_topic(session)).get(topic_id, [])

    await session.execute(delete(TopicRole).where(TopicRole.topic_id == topic_id))
    for role_code in sorted(set(payload.roles)):
        session.add(TopicRole(topic_id=topic_id, role_code=role_code))
    await session.flush()

    await write_audit(
        session,
        action="topic.roles.set",
        entity_type="topic",
        entity_id=str(topic_id),
        actor_id=admin.id,
        old_value={"roles": before},
        new_value={"roles": sorted(set(payload.roles))},
        request_id=getattr(request.state, "request_id", None),
    )
    await session.commit()
    return _to_response(topic, sorted(set(payload.roles)), "available")
