"""Схемы тем и подписок."""

from datetime import datetime

from pydantic import Field

from app.schemas.base import ApiModel
from app.services.topics import MatchRule


class TopicCreate(ApiModel):
    code: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9\-_]*$")
    name: str = Field(min_length=1, max_length=256)
    description: str | None = Field(default=None, max_length=2000)
    match_rule: MatchRule = Field(default_factory=MatchRule)


class TopicUpdate(ApiModel):
    name: str | None = Field(default=None, min_length=1, max_length=256)
    description: str | None = Field(default=None, max_length=2000)
    match_rule: MatchRule | None = None
    is_active: bool | None = None


class TopicRolesUpdate(ApiModel):
    """Привязка темы к ролям делает подписку обязательной для их носителей."""

    roles: list[str]


class TopicResponse(ApiModel):
    id: int
    code: str
    name: str
    description: str | None
    match_rule: MatchRule
    is_active: bool
    roles: list[str]
    # mandatory | subscribed | available — состояние для текущего пользователя
    state: str


class SubscriptionResponse(ApiModel):
    topic_id: int
    topic_code: str
    topic_name: str
    topic_description: str | None
    origin: str
    channels: list[str]
    muted_until: datetime | None
    is_mandatory: bool
    subscription_id: int | None
    # Можно ли отписаться самому: только у origin=self
    can_unsubscribe: bool


class SubscriptionsResponse(ApiModel):
    """Экран подписок: две вкладки и ничего больше (раздел 4 AGENTS.md)."""

    mandatory: list[SubscriptionResponse]
    voluntary: list[SubscriptionResponse]


class SubscriptionCreate(ApiModel):
    topic_id: int
    channels: list[str] = Field(default_factory=lambda: ["web"])


class SubscriptionUpdate(ApiModel):
    channels: list[str] | None = None
    muted_until: datetime | None = None


class AdminSubscriptionCreate(ApiModel):
    user_id: int
    topic_id: int
    channels: list[str] = Field(default_factory=lambda: ["web"])
