"""Контракт провайдера аутентификации.

ПДн-политика (раздел 5 AGENTS.md): из каталога берутся только непрозрачный
идентификатор и членство в группах. ФИО, телефон и email не сохраняются —
отображаемое имя живёт в объекте Subject ровно на время запроса.
"""

from dataclasses import dataclass, field
from typing import Protocol


class IdentityError(Exception):
    """Общая ошибка провайдера."""


class DirectoryUnavailableError(IdentityError):
    """Каталог недоступен. Не повод ронять систему: роли берутся из кеша."""


@dataclass
class Subject:
    """Субъект каталога. display_name не персистится — только для ответа API."""

    subject_id: str
    username: str
    roles: list[str] = field(default_factory=list)
    display_name: str | None = None


class IdentityProvider(Protocol):
    name: str

    async def authenticate(self, username: str, password: str) -> Subject | None: ...

    async def get_roles(self, subject_id: str) -> list[str]: ...

    async def is_active(self, subject_id: str) -> bool: ...
