"""Модели данных. Источник истины по схеме — `db/schema.sql`.

Импорт всех модулей здесь обязателен: Base.metadata должна знать обо всех
таблицах (нужно для autogenerate в Alembic).
"""

from app.models.base import Base
from app.models.delivery import LlmSummary, Notification
from app.models.events import (
    EventSource,
    Incident,
    IncidentEvent,
    MonitoredObject,
    RawEvent,
)
from app.models.identity import (
    Role,
    RoleMapping,
    Session,
    User,
    UserContact,
    UserRole,
)
from app.models.subscriptions import Subscription, Topic, TopicRole
from app.models.system import AuditLog, SeverityMap

__all__ = [
    "AuditLog",
    "Base",
    "EventSource",
    "Incident",
    "IncidentEvent",
    "LlmSummary",
    "MonitoredObject",
    "Notification",
    "RawEvent",
    "Role",
    "RoleMapping",
    "Session",
    "SeverityMap",
    "Subscription",
    "Topic",
    "TopicRole",
    "User",
    "UserContact",
    "UserRole",
]
