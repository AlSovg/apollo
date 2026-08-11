"""Журнал аудита.

Требование ИБ: изменения тем, ролей, подписок, пользователей и источников
прослеживаемы, действия администраторов журналируются наравне с пользовательскими.

Сюда НЕ пишутся адреса доставки, тела уведомлений и payload событий — журнал
не должен становиться обходным хранилищем ПДн.
"""

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.system import AuditLog


async def write_audit(
    session: AsyncSession,
    *,
    action: str,
    entity_type: str,
    entity_id: str | None = None,
    actor_id: int | None = None,
    old_value: dict[str, Any] | None = None,
    new_value: dict[str, Any] | None = None,
    request_id: str | None = None,
) -> None:
    session.add(
        AuditLog(
            actor_id=actor_id,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            old_value=old_value,
            new_value=new_value,
            request_id=request_id,
        )
    )
    await session.flush()
