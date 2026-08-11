"""Резолвер адресов доставки.

Адрес получателя НИКОГДА не пишется в notifications и не попадает в логи:
он резолвится в момент отправки и живёт только в памяти обработчика (FR-DLV-4).
Это же и есть причина, по которой в схеме нет колонки с адресом.
"""

from typing import Protocol


class ContactResolver(Protocol):
    name: str

    async def resolve(self, user_id: int, channel: str) -> str | None: ...
