"""Схемы административных экранов: пользователи и аудит.

ПДн-политика соблюдается и здесь: наружу уходят subject_id, логин и роли.
ФИО, email и телефон в системе не хранятся, показывать нечего.
"""

from datetime import datetime
from typing import Any

from pydantic import Field

from app.schemas.base import ApiModel


class UserRoleItem(ApiModel):
    code: str
    # local — назначил администратор, ldap — пришло из каталога
    source: str


class UserResponse(ApiModel):
    id: int
    subject_id: str
    provider: str
    username: str
    is_active: bool
    roles: list[UserRoleItem]
    locked_until: datetime | None
    roles_synced_at: datetime | None
    last_login_at: datetime | None
    created_at: datetime


class UserPage(ApiModel):
    items: list[UserResponse]
    total: int
    page: int
    size: int


class UserRolesUpdate(ApiModel):
    """Локальные роли. Роли из каталога синхронизация перезапишет сама."""

    roles: list[str] = Field(default_factory=list)


class DeactivateResult(ApiModel):
    user_id: int
    is_active: bool
    sessions_revoked: int


class AuditEntry(ApiModel):
    id: int
    actor_id: int | None
    actor_username: str | None
    action: str
    entity_type: str
    entity_id: str | None
    old_value: dict[str, Any] | None
    new_value: dict[str, Any] | None
    request_id: str | None
    created_at: datetime


class AuditPage(ApiModel):
    items: list[AuditEntry]
    total: int
    page: int
    size: int
