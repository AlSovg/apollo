"""Схемы аутентификации.

Токены в теле ответа не возвращаются: они живут только в httpOnly-cookie.
"""

from pydantic import Field

from app.schemas.base import ApiModel


class LoginRequest(ApiModel):
    username: str = Field(min_length=1, max_length=256)
    password: str = Field(min_length=1, max_length=1024)


class SessionStatus(ApiModel):
    status: str


class CurrentUserResponse(ApiModel):
    subject_id: str
    username: str
    roles: list[str]
    # Подтягивается из каталога в момент запроса и в БД не пишется (ПДн)
    display_name: str | None = None


class SyncResult(ApiModel):
    checked: int
    updated: int
    deactivated: int
