"""Зависимости доступа: текущий пользователь и проверка ролей."""

from collections.abc import Callable, Coroutine
from typing import Any

from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.errors import ApiError
from app.config import get_settings
from app.db import get_session
from app.models.identity import User, UserRole
from app.services.auth import ACCESS_COOKIE, TOKEN_TYPE_ACCESS, InvalidTokenError, decode_token


class CurrentUser:
    def __init__(self, user: User, roles: list[str]) -> None:
        self.user = user
        self.roles = roles

    @property
    def id(self) -> int:
        return self.user.id

    @property
    def subject_id(self) -> str:
        return self.user.subject_id

    def has_role(self, *codes: str) -> bool:
        return any(code in self.roles for code in codes)


async def current_user(
    request: Request, session: AsyncSession = Depends(get_session)
) -> CurrentUser:
    """Пользователь по access-cookie.

    Активность и роли читаются из БД на каждый запрос, а не берутся из токена:
    иначе отключённая учётка сохраняла бы доступ до истечения токена, и
    требование FR-IDM-4 выполнялось бы только на бумаге.
    """
    token = request.cookies.get(ACCESS_COOKIE)
    if not token:
        raise InvalidTokenError("Требуется аутентификация")

    payload = decode_token(
        token, secret=get_settings().jwt_secret, expected_type=TOKEN_TYPE_ACCESS
    )
    user = (
        await session.execute(select(User).where(User.subject_id == payload["sub"]))
    ).scalar_one_or_none()
    if user is None or not user.is_active:
        raise InvalidTokenError("Учётная запись недоступна")

    roles = list(
        (
            await session.execute(select(UserRole.role_code).where(UserRole.user_id == user.id))
        ).scalars()
    )
    return CurrentUser(user, sorted(roles))


def require_roles(
    *codes: str,
) -> Callable[[CurrentUser], Coroutine[Any, Any, CurrentUser]]:
    """RBAC: матрица доступа — в разделе 2 docs/tz.md."""

    async def dependency(user: CurrentUser = Depends(current_user)) -> CurrentUser:
        if not user.has_role(*codes):
            raise ApiError(403, "FORBIDDEN", "Недостаточно прав")
        return user

    return dependency
