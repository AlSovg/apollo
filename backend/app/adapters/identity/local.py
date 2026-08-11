"""Локальный провайдер: служебные учётки и аварийный вход администратора.

Не удалять при переходе на каталог: иначе отказ AD запирает вход в саму
платформу, и чинить её будет некому (раздел 5.3 docs/tz.md).
"""

import logging

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.identity.base import IdentityProvider, Subject
from app.models.identity import User, UserRole

logger = logging.getLogger(__name__)

# argon2id — вариант по умолчанию в argon2-cffi
_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, InvalidHashError):
        return False


class LocalIdentityProvider(IdentityProvider):
    name = "local"

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def _user(self, username: str) -> User | None:
        return (
            await self._session.execute(
                select(User).where(User.username == username, User.provider == "local")
            )
        ).scalar_one_or_none()

    async def authenticate(self, username: str, password: str) -> Subject | None:
        user = await self._user(username)
        if user is None or user.password_hash is None:
            # Пароль всё равно «проверяем», чтобы время ответа не выдавало
            # существование учётной записи
            _hasher.hash(password or "x")
            return None
        if not verify_password(user.password_hash, password):
            return None
        return Subject(
            subject_id=user.subject_id,
            username=user.username,
            roles=await self.get_roles(user.subject_id),
        )

    async def get_roles(self, subject_id: str) -> list[str]:
        rows = (
            await self._session.execute(
                select(UserRole.role_code)
                .join(User, User.id == UserRole.user_id)
                .where(User.subject_id == subject_id, User.provider == "local")
            )
        ).scalars()
        return list(rows)

    async def is_active(self, subject_id: str) -> bool:
        user = (
            await self._session.execute(
                select(User).where(User.subject_id == subject_id, User.provider == "local")
            )
        ).scalar_one_or_none()
        return bool(user and user.is_active)
