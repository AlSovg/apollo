"""Доставка уведомлений.

Очередь — та же таблица notifications, разбирается через FOR UPDATE SKIP LOCKED
(запрос зафиксирован в конце db/schema.sql). Несколько воркеров работают
параллельно без координатора и без дублей.

Адрес получателя резолвится здесь, непосредственно перед отправкой, и никуда
не записывается — ни в БД, ни в логи (FR-DLV-4).
"""

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import Select, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.channels import (
    NotificationChannel,
    NotificationMessage,
    PermanentChannelError,
    TransientChannelError,
)
from app.adapters.contacts import ContactResolver
from app.models.delivery import LlmSummary, Notification
from app.models.events import Incident, MonitoredObject

logger = logging.getLogger(__name__)

DEFAULT_RETRY_DELAYS = (1, 5, 30)


@dataclass
class DeliveryResult:
    sent: int = 0
    failed: int = 0
    retried: int = 0
    processed: list[int] = field(default_factory=list)

    @property
    def size(self) -> int:
        return len(self.processed)


def select_pending(limit: int, *, now: datetime) -> Select[tuple[Notification]]:
    return (
        select(Notification)
        .where(
            Notification.status == "pending",
            (Notification.next_retry_at.is_(None)) | (Notification.next_retry_at <= now),
        )
        .order_by(Notification.created_at)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )


async def build_message(
    session: AsyncSession, notification: Notification
) -> NotificationMessage:
    """Текст собирается из инцидента: в notifications содержимого нет."""
    row = (
        await session.execute(
            select(Incident, MonitoredObject.name, LlmSummary.content)
            .join(MonitoredObject, MonitoredObject.id == Incident.object_id, isouter=True)
            .join(LlmSummary, LlmSummary.incident_id == Incident.id, isouter=True)
            .where(Incident.id == notification.incident_id)
            .order_by(LlmSummary.created_at.desc())
            .limit(1)
        )
    ).first()
    if row is None:
        raise PermanentChannelError("инцидент уведомления не найден")

    incident, object_name, summary = row
    return NotificationMessage(
        notification_id=notification.id,
        incident_id=incident.id,
        severity=incident.severity,
        rule_name=incident.rule_name,
        object_name=object_name,
        opened_at=incident.opened_at,
        message=incident.last_message,
        summary=summary,
        url=incident.url,
    )


def next_retry_at(attempts: int, *, now: datetime, delays: list[int]) -> datetime | None:
    """Экспоненциальная задержка. None — попытки исчерпаны."""
    if attempts > len(delays):
        return None
    return now + timedelta(minutes=delays[attempts - 1])


async def deliver_one(
    session: AsyncSession,
    notification: Notification,
    *,
    channels: dict[str, NotificationChannel],
    contacts: ContactResolver,
    now: datetime,
    delays: list[int],
) -> str:
    """Одна доставка. Возвращает итоговый статус: sent | failed | pending."""
    attempts = notification.attempts + 1
    channel = channels.get(notification.channel)

    if channel is None:
        await _mark(session, notification, status="failed", attempts=attempts,
                    error=f"канал '{notification.channel}' не зарегистрирован")
        return "failed"

    try:
        message = await build_message(session, notification)
        address = await contacts.resolve(notification.user_id, notification.channel)
        await channel.send(message, address)
    except PermanentChannelError as exc:
        await _mark(session, notification, status="failed", attempts=attempts, error=str(exc))
        return "failed"
    except (TransientChannelError, TimeoutError, OSError) as exc:
        retry_at = next_retry_at(attempts, now=now, delays=delays)
        status = "pending" if retry_at is not None else "failed"
        await _mark(
            session,
            notification,
            status=status,
            attempts=attempts,
            error=str(exc),
            retry_at=retry_at,
        )
        return status

    await _mark(session, notification, status="sent", attempts=attempts, sent_at=now)
    return "sent"


async def _mark(
    session: AsyncSession,
    notification: Notification,
    *,
    status: str,
    attempts: int,
    error: str | None = None,
    retry_at: datetime | None = None,
    sent_at: datetime | None = None,
) -> None:
    values: dict[str, object] = {
        "status": status,
        "attempts": attempts,
        "last_error": error[:1000] if error else None,
        "next_retry_at": retry_at,
    }
    if sent_at is not None:
        values["sent_at"] = sent_at
    await session.execute(
        update(Notification).where(Notification.id == notification.id).values(**values)
    )


async def deliver_batch(
    session: AsyncSession,
    *,
    channels: dict[str, NotificationChannel],
    contacts: ContactResolver,
    limit: int = 50,
    now: datetime | None = None,
    delays: list[int] | None = None,
) -> DeliveryResult:
    """Разобрать пачку уведомлений в статусе pending."""
    moment = now or datetime.now(tz=UTC)
    retry_delays = delays or list(DEFAULT_RETRY_DELAYS)
    result = DeliveryResult()

    pending = (await session.execute(select_pending(limit, now=moment))).scalars().all()
    for notification in pending:
        async with session.begin_nested():
            status = await deliver_one(
                session,
                notification,
                channels=channels,
                contacts=contacts,
                now=moment,
                delays=retry_delays,
            )
        result.processed.append(notification.id)
        if status == "sent":
            result.sent += 1
        elif status == "failed":
            result.failed += 1
        else:
            result.retried += 1

        # В лог — идентификатор и статус: ни адреса, ни текста уведомления
        logger.info(
            "notification processed",
            extra={
                "notification_id": notification.id,
                "channel": notification.channel,
                "status": status,
            },
        )
    return result
