"""Управление пользователями и журнал аудита. Доступ — только admin."""

import logging

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import Select, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, require_roles
from app.api.errors import ApiError
from app.db import get_session
from app.models.identity import Role, User, UserRole
from app.models.system import AuditLog
from app.schemas.admin import (
    AuditEntry,
    AuditPage,
    DeactivateResult,
    UserPage,
    UserResponse,
    UserRoleItem,
    UserRolesUpdate,
)
from app.services.audit import write_audit
from app.services.users import deactivate_user, get_user_roles, revoke_sessions

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])


async def _roles_by_user(
    session: AsyncSession, user_ids: list[int]
) -> dict[int, list[UserRoleItem]]:
    if not user_ids:
        return {}
    rows = (
        await session.execute(select(UserRole).where(UserRole.user_id.in_(user_ids)))
    ).scalars().all()

    mapping: dict[int, list[UserRoleItem]] = {}
    for row in rows:
        mapping.setdefault(row.user_id, []).append(
            UserRoleItem(code=row.role_code, source=row.source)
        )
    return {
        user_id: sorted(items, key=lambda item: item.code)
        for user_id, items in mapping.items()
    }


def _to_response(user: User, roles: list[UserRoleItem]) -> UserResponse:
    return UserResponse(
        id=user.id,
        subject_id=user.subject_id,
        provider=user.provider,
        username=user.username,
        is_active=user.is_active,
        roles=roles,
        locked_until=user.locked_until,
        roles_synced_at=user.roles_synced_at,
        last_login_at=user.last_login_at,
        created_at=user.created_at,
    )


@router.get("/users", response_model=UserPage, summary="Пользователи и их роли")
async def list_users(
    query: str | None = Query(default=None, max_length=128),
    is_active: bool | None = None,
    provider: str | None = Query(default=None, pattern="^(local|ldap)$"),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=100),
    admin: CurrentUser = Depends(require_roles("admin")),
    session: AsyncSession = Depends(get_session),
) -> UserPage:
    statement: Select[tuple[User]] = select(User)
    total_statement = select(func.count()).select_from(User)

    if query:
        condition = User.username.ilike(f"%{query}%")
        statement = statement.where(condition)
        total_statement = total_statement.where(condition)
    if is_active is not None:
        statement = statement.where(User.is_active.is_(is_active))
        total_statement = total_statement.where(User.is_active.is_(is_active))
    if provider is not None:
        statement = statement.where(User.provider == provider)
        total_statement = total_statement.where(User.provider == provider)

    users = (
        await session.execute(
            statement.order_by(User.username).limit(size).offset((page - 1) * size)
        )
    ).scalars().all()
    total = await session.scalar(total_statement)

    roles = await _roles_by_user(session, [user.id for user in users])
    return UserPage(
        items=[_to_response(user, roles.get(user.id, [])) for user in users],
        total=int(total or 0),
        page=page,
        size=size,
    )


@router.put(
    "/users/{user_id}/roles", response_model=UserResponse, summary="Локальные роли"
)
async def set_user_roles(
    user_id: int,
    payload: UserRolesUpdate,
    request: Request,
    admin: CurrentUser = Depends(require_roles("admin")),
    session: AsyncSession = Depends(get_session),
) -> UserResponse:
    """Назначает роли с source='local'.

    Роли из каталога не трогаем: их перезапишет ближайшая синхронизация,
    и ручная правка всё равно бы пропала (FR-IDM-2).
    """
    user = await session.get(User, user_id)
    if user is None:
        raise ApiError(404, "USER_NOT_FOUND", "Пользователь не найден")

    known = set((await session.execute(select(Role.code))).scalars().all())
    requested = sorted(set(payload.roles))
    unknown = [code for code in requested if code not in known]
    if unknown:
        raise ApiError(400, "UNKNOWN_ROLE", f"Неизвестные роли: {', '.join(unknown)}")

    if user.id == admin.id and "admin" not in requested:
        # Роль из каталога останется и после правки локальных — тогда доступ
        # не теряется и запрещать нечего
        from_directory = set(
            (
                await session.execute(
                    select(UserRole.role_code).where(
                        UserRole.user_id == user_id, UserRole.source == "ldap"
                    )
                )
            )
            .scalars()
            .all()
        )
        if "admin" not in from_directory:
            # Иначе администратор снимает себе доступ и запирает администрирование
            raise ApiError(
                409, "SELF_ADMIN_LOSS", "Нельзя снять с себя роль администратора"
            )

    before = await get_user_roles(session, user_id)

    await session.execute(
        delete(UserRole).where(UserRole.user_id == user_id, UserRole.source == "local")
    )
    for code in requested:
        session.add(UserRole(user_id=user_id, role_code=code, source="local"))
    await session.flush()

    after = await get_user_roles(session, user_id)
    await write_audit(
        session,
        action="user.roles.set",
        entity_type="user",
        entity_id=str(user_id),
        actor_id=admin.id,
        old_value={"roles": before},
        new_value={"roles": after},
        request_id=getattr(request.state, "request_id", None),
    )
    await session.commit()

    roles = await _roles_by_user(session, [user_id])
    return _to_response(user, roles.get(user_id, []))


@router.post(
    "/users/{user_id}/deactivate",
    response_model=DeactivateResult,
    summary="Отключение учётной записи",
)
async def deactivate(
    user_id: int,
    request: Request,
    admin: CurrentUser = Depends(require_roles("admin")),
    session: AsyncSession = Depends(get_session),
) -> DeactivateResult:
    """Доступ закрывается сразу, подписки сохраняются (FR-IDM-4)."""
    user = await session.get(User, user_id)
    if user is None:
        raise ApiError(404, "USER_NOT_FOUND", "Пользователь не найден")
    if user.id == admin.id:
        # Иначе администратор запирает сам себя
        raise ApiError(409, "SELF_DEACTIVATION", "Нельзя отключить собственную учётку")

    revoked = await revoke_sessions(session, user_id) if user.is_active else 0
    await deactivate_user(
        session,
        user,
        reason="отключена администратором",
        actor_id=admin.id,
        request_id=getattr(request.state, "request_id", None),
    )
    await session.commit()

    return DeactivateResult(user_id=user_id, is_active=False, sessions_revoked=revoked)


@router.get("/audit", response_model=AuditPage, summary="Журнал аудита")
async def audit_log(
    actor_id: int | None = None,
    entity_type: str | None = Query(default=None, max_length=64),
    action: str | None = Query(default=None, max_length=64),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=50, ge=1, le=200),
    admin: CurrentUser = Depends(require_roles("admin")),
    session: AsyncSession = Depends(get_session),
) -> AuditPage:
    """Действия администраторов журналируются наравне с пользовательскими."""
    statement = select(AuditLog, User.username).join(
        User, User.id == AuditLog.actor_id, isouter=True
    )
    total_statement = select(func.count()).select_from(AuditLog)

    if actor_id is not None:
        statement = statement.where(AuditLog.actor_id == actor_id)
        total_statement = total_statement.where(AuditLog.actor_id == actor_id)
    if entity_type is not None:
        statement = statement.where(AuditLog.entity_type == entity_type)
        total_statement = total_statement.where(AuditLog.entity_type == entity_type)
    if action is not None:
        statement = statement.where(AuditLog.action == action)
        total_statement = total_statement.where(AuditLog.action == action)

    rows = (
        await session.execute(
            statement.order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
            .limit(size)
            .offset((page - 1) * size)
        )
    ).all()
    total = await session.scalar(total_statement)

    return AuditPage(
        items=[
            AuditEntry(
                id=entry.id,
                actor_id=entry.actor_id,
                actor_username=username,
                action=entry.action,
                entity_type=entry.entity_type,
                entity_id=entry.entity_id,
                old_value=entry.old_value,
                new_value=entry.new_value,
                request_id=entry.request_id,
                created_at=entry.created_at,
            )
            for entry, username in rows
        ],
        total=int(total or 0),
        page=page,
        size=size,
    )
