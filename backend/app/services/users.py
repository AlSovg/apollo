"""Пользователи, роли и жизненный цикл учётных записей (FR-IDM).

Ключевое свойство: обязательные подписки нигде не материализуются, поэтому
смена должности не требует правки таблицы subscriptions — достаточно
пересчитать роли, и эффективные подписки изменятся сами (см. шаг 6).
"""

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.identity import Subject
from app.models.identity import Role, RoleMapping, Session, User, UserRole
from app.services.audit import write_audit

logger = logging.getLogger(__name__)


async def map_groups_to_roles(session: AsyncSession, groups: list[str]) -> list[str]:
    """DN групп каталога → внутренние роли через role_mappings.

    Сравнение регистронезависимое: AD отдаёт DN в произвольном регистре.
    """
    if not groups:
        return []
    rows = (await session.execute(select(RoleMapping))).scalars().all()
    normalized = {group.strip().lower() for group in groups}
    return sorted(
        {row.role_code for row in rows if row.external_group.strip().lower() in normalized}
    )


async def get_user_roles(session: AsyncSession, user_id: int) -> list[str]:
    rows = (
        await session.execute(select(UserRole.role_code).where(UserRole.user_id == user_id))
    ).scalars()
    return sorted(rows)


async def upsert_user(
    session: AsyncSession, subject: Subject, provider: str
) -> User:
    """Создать или обновить учётку. ФИО, email и телефон не сохраняются."""
    statement = (
        insert(User)
        .values(
            subject_id=subject.subject_id,
            provider=provider,
            username=subject.username,
            is_active=True,
        )
        .on_conflict_do_update(
            constraint="users_subject_uq",
            set_={"username": subject.username, "is_active": True},
        )
        .returning(User.id)
    )
    user_id = int((await session.execute(statement)).scalar_one())
    return (await session.execute(select(User).where(User.id == user_id))).scalar_one()


async def sync_roles(
    session: AsyncSession,
    user: User,
    directory_roles: list[str],
    *,
    actor_id: int | None = None,
    request_id: str | None = None,
) -> list[str]:
    """Привести роли из каталога к текущему состоянию.

    Роли с source='local' назначает администратор — синхронизация их не трогает.
    Любое изменение состава ролей попадает в audit_log (FR-IDM-6).
    """
    known = set(
        (await session.execute(select(Role.code))).scalars().all()
    )
    desired = {role for role in directory_roles if role in known}

    existing = (
        await session.execute(select(UserRole).where(UserRole.user_id == user.id))
    ).scalars().all()
    before = sorted(row.role_code for row in existing)
    from_directory = {row.role_code for row in existing if row.source == "ldap"}

    for role_code in from_directory - desired:
        await session.execute(
            delete(UserRole).where(
                UserRole.user_id == user.id,
                UserRole.role_code == role_code,
                UserRole.source == "ldap",
            )
        )
    for role_code in desired - {row.role_code for row in existing}:
        session.add(
            UserRole(user_id=user.id, role_code=role_code, source="ldap")
        )

    await session.flush()
    after = await get_user_roles(session, user.id)

    if before != after:
        await write_audit(
            session,
            action="user.roles.sync",
            entity_type="user",
            entity_id=str(user.id),
            actor_id=actor_id,
            old_value={"roles": before},
            new_value={"roles": after},
            request_id=request_id,
        )
        logger.info(
            "roles synchronised", extra={"user_id": user.id, "roles_count": len(after)}
        )

    await session.execute(
        update(User).where(User.id == user.id).values(roles_synced_at=datetime.now(tz=UTC))
    )
    return after


async def revoke_sessions(session: AsyncSession, user_id: int) -> int:
    """Отозвать все refresh-сессии пользователя."""
    active = (
        await session.execute(
            select(Session.id).where(
                Session.user_id == user_id, Session.revoked_at.is_(None)
            )
        )
    ).scalars().all()
    if not active:
        return 0

    await session.execute(
        update(Session)
        .where(Session.id.in_(active))
        .values(revoked_at=datetime.now(tz=UTC))
    )
    return len(active)


async def deactivate_user(
    session: AsyncSession,
    user: User,
    *,
    reason: str,
    actor_id: int | None = None,
    request_id: str | None = None,
) -> None:
    """Учётка отключена в каталоге → доступ закрыт (FR-IDM-4).

    Подписки НЕ удаляются: при восстановлении учётки они возвращаются.
    """
    if not user.is_active:
        return

    await session.execute(update(User).where(User.id == user.id).values(is_active=False))
    revoked = await revoke_sessions(session, user.id)
    await write_audit(
        session,
        action="user.deactivate",
        entity_type="user",
        entity_id=str(user.id),
        actor_id=actor_id,
        old_value={"is_active": True},
        new_value={"is_active": False, "reason": reason, "sessions_revoked": revoked},
        request_id=request_id,
    )
    logger.info(
        "user deactivated", extra={"user_id": user.id, "sessions_revoked": revoked}
    )


async def register_failed_login(
    session: AsyncSession, user: User, *, max_attempts: int, lockout: timedelta
) -> None:
    """Блокировка после N неудач — только для локального провайдера."""
    failed = user.failed_logins + 1
    values: dict[str, object] = {"failed_logins": failed}
    if failed >= max_attempts:
        values["locked_until"] = datetime.now(tz=UTC) + lockout
    await session.execute(update(User).where(User.id == user.id).values(**values))


async def register_successful_login(session: AsyncSession, user: User) -> None:
    await session.execute(
        update(User)
        .where(User.id == user.id)
        .values(failed_logins=0, locked_until=None, last_login_at=datetime.now(tz=UTC))
    )


def is_locked(user: User, now: datetime | None = None) -> bool:
    moment = now or datetime.now(tz=UTC)
    return user.locked_until is not None and user.locked_until > moment
