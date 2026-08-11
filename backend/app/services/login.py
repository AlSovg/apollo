"""Сценарий входа и синхронизация учёток с каталогом.

Порядок для AUTH_PROVIDER=ldap: сначала каталог; если он недоступен,
остаётся локальный провайдер — иначе отказ AD запирает вход в саму платформу
(FR-IDM-7). Пароль пользователя каталога нигде не сохраняется.
"""

import logging
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.identity import (
    DirectoryUnavailableError,
    LdapIdentityProvider,
    LocalIdentityProvider,
    Subject,
    ldap_settings,
)
from app.api.errors import ApiError
from app.config import Settings
from app.models.identity import User
from app.services.audit import write_audit
from app.services.users import (
    deactivate_user,
    get_user_roles,
    is_locked,
    map_groups_to_roles,
    register_failed_login,
    register_successful_login,
    sync_roles,
    upsert_user,
)

logger = logging.getLogger(__name__)


def _unauthorized() -> ApiError:
    # Причина не раскрывается: нет учётки, неверный пароль — ответ одинаковый
    return ApiError(401, "UNAUTHORIZED", "Неверный логин или пароль")


async def _local_login(
    session: AsyncSession, username: str, password: str, settings: Settings, request_id: str | None
) -> tuple[Subject, User]:
    """Вход локальной учётки.

    Локальные пользователи заводятся администратором, поэтому вход их
    НЕ создаёт: у такой записи обязан быть пароль (users_password_chk).
    """
    user = (
        await session.execute(
            select(User).where(User.username == username, User.provider == "local")
        )
    ).scalar_one_or_none()

    if user is not None and is_locked(user):
        raise ApiError(
            401, "ACCOUNT_LOCKED", "Учётная запись временно заблокирована"
        )
    if user is not None and not user.is_active:
        raise _unauthorized()

    subject = None if user is None else await LocalIdentityProvider(session).authenticate(
        username, password
    )
    if subject is None or user is None:
        if user is not None:
            await register_failed_login(
                session,
                user,
                max_attempts=settings.max_failed_logins,
                lockout=timedelta(minutes=settings.lockout_minutes),
            )
        await write_audit(
            session,
            action="user.login.failed",
            entity_type="user",
            entity_id=str(user.id) if user else None,
            actor_id=user.id if user else None,
            new_value={"username": username, "provider": "local"},
            request_id=request_id,
        )
        raise _unauthorized()

    await register_successful_login(session, user)
    return subject, user


async def login(
    session: AsyncSession,
    *,
    username: str,
    password: str,
    settings: Settings,
    request_id: str | None = None,
) -> tuple[User, list[str]]:
    """Аутентификация и подготовка учётки. Возвращает пользователя и его роли."""
    subject: Subject | None = None
    user: User | None = None
    provider = "local"
    degraded = False

    if settings.auth_provider == "ldap":
        try:
            subject = await LdapIdentityProvider(ldap_settings(settings)).authenticate(
                username, password
            )
            provider = "ldap"
        except DirectoryUnavailableError:
            # Каталог лежит — остаётся аварийный вход локальных учёток
            degraded = True
            logger.warning("auth: каталог недоступен, вход через локальный провайдер")

        if subject is None and not degraded:
            await write_audit(
                session,
                action="user.login.failed",
                entity_type="user",
                new_value={"username": username, "provider": "ldap"},
                request_id=request_id,
            )
            raise _unauthorized()

    if subject is None:
        subject, user = await _local_login(session, username, password, settings, request_id)
        provider = "local"

    if provider == "ldap":
        # Доменная учётка заводится сама при первом входе: ФИО, email
        # и телефон при этом не сохраняются (раздел 5 AGENTS.md)
        user = await upsert_user(session, subject, provider)
        # subject.roles здесь — DN групп каталога, роли из них делает role_mappings
        roles = await sync_roles(
            session,
            user,
            await map_groups_to_roles(session, subject.roles),
            request_id=request_id,
        )
        await register_successful_login(session, user)
    else:
        assert user is not None  # локальный вход всегда возвращает учётку
        roles = sorted(subject.roles)

    await write_audit(
        session,
        action="user.login",
        entity_type="user",
        entity_id=str(user.id),
        actor_id=user.id,
        new_value={"provider": provider, "degraded": degraded},
        request_id=request_id,
    )
    return user, roles


async def sync_directory(
    session: AsyncSession,
    settings: Settings,
    *,
    actor_id: int | None = None,
    request_id: str | None = None,
) -> dict[str, int]:
    """Синхронизация ролей и состояния всех доменных учёток (FR-IDM-3..5).

    Строки в subscriptions не трогаются вообще: обязательные подписки
    вычисляются из ролей, поэтому смена должности отрабатывает сама.
    """
    if settings.auth_provider != "ldap":
        raise ApiError(409, "AUTH_PROVIDER_NOT_LDAP", "Каталог не используется")

    provider = LdapIdentityProvider(ldap_settings(settings))
    users = (
        (await session.execute(select(User).where(User.provider == "ldap"))).scalars().all()
    )

    stats = {"checked": 0, "updated": 0, "deactivated": 0}
    for user in users:
        stats["checked"] += 1
        try:
            active = await provider.is_active(user.subject_id)
        except DirectoryUnavailableError:
            # Деградация каталога не должна массово отключать людям доступ
            logger.warning("ldap sync: каталог недоступен, синхронизация прервана")
            break

        if not active:
            await deactivate_user(
                session,
                user,
                reason="отсутствует или отключена в каталоге",
                actor_id=actor_id,
                request_id=request_id,
            )
            stats["deactivated"] += 1
            continue

        groups = await provider.get_roles(user.subject_id)
        before = set(await get_user_roles(session, user.id))
        after = set(
            await sync_roles(
                session,
                user,
                await map_groups_to_roles(session, groups),
                actor_id=actor_id,
                request_id=request_id,
            )
        )
        if before != after:
            stats["updated"] += 1

    return stats
