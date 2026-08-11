"""Локальный резолвер — по таблице user_contacts.

Существует только пока нет каталога. Таблица помечена как содержащая ПДн
и очищается при переключении на LDAP (раздел 5 AGENTS.md).
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.identity import UserContact


class LocalContactResolver:
    name = "local"

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def resolve(self, user_id: int, channel: str) -> str | None:
        if channel == "web":
            return None  # веб-инбоксу адрес не нужен
        return (
            await self._session.execute(
                select(UserContact.address).where(
                    UserContact.user_id == user_id, UserContact.channel == channel
                )
            )
        ).scalar_one_or_none()
