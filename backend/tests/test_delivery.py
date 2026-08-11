"""Доставка: ретраи, отказы, параллельные воркеры."""

import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.adapters.channels import (
    NotificationMessage,
    PermanentChannelError,
    TransientChannelError,
    WebChannel,
)
from app.adapters.contacts import LocalContactResolver
from app.models.delivery import Notification
from app.models.identity import UserContact
from app.services.delivery import deliver_batch, next_retry_at
from app.services.router import route_batch
from tests.factories import make_incident, make_topic, make_user, subscribe

DELAYS = [1, 5, 30]


class RecordingChannel:
    """Канал, который всё принимает и запоминает — подстановка для проверок."""

    name = "web"

    def __init__(self) -> None:
        self.sent: list[tuple[int, str | None]] = []

    async def send(self, message: NotificationMessage, address: str | None) -> None:
        self.sent.append((message.notification_id, address))


class FailingChannel:
    name = "email"

    def __init__(self, error: Exception) -> None:
        self._error = error
        self.attempts = 0

    async def send(self, message: NotificationMessage, address: str | None) -> None:
        self.attempts += 1
        raise self._error


async def _one_pending(db_session: AsyncSession, channel: str = "web") -> Notification:
    user = await make_user(db_session, "duty")
    topic = await make_topic(db_session)
    await subscribe(db_session, user, topic, channels=[channel])
    await make_incident(db_session)
    await route_batch(db_session)
    return (await db_session.execute(select(Notification))).scalar_one()


async def test_successful_delivery_marks_sent(db_session: AsyncSession) -> None:
    notification = await _one_pending(db_session)
    channel = RecordingChannel()

    result = await deliver_batch(
        db_session,
        channels={"web": channel},
        contacts=LocalContactResolver(db_session),
        delays=DELAYS,
    )

    assert result.sent == 1
    await db_session.refresh(notification)
    assert notification.status == "sent"
    assert notification.sent_at is not None
    assert notification.attempts == 1
    assert notification.last_error is None


async def test_message_is_built_from_incident(db_session: AsyncSession) -> None:
    """В notifications текста нет — он собирается из инцидента."""
    await _one_pending(db_session)
    channel = RecordingChannel()

    await deliver_batch(
        db_session,
        channels={"web": channel},
        contacts=LocalContactResolver(db_session),
        delays=DELAYS,
    )

    assert len(channel.sent) == 1
    # Веб-инбоксу адрес не нужен
    assert channel.sent[0][1] is None


async def test_transient_failure_schedules_retry(db_session: AsyncSession) -> None:
    notification = await _one_pending(db_session, channel="email")
    channel = FailingChannel(TransientChannelError("SMTP недоступен: TimeoutError"))
    moment = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)

    result = await deliver_batch(
        db_session,
        channels={"email": channel},
        contacts=LocalContactResolver(db_session),
        now=moment,
        delays=DELAYS,
    )

    assert result.retried == 1
    await db_session.refresh(notification)
    assert notification.status == "pending"
    assert notification.attempts == 1
    assert notification.next_retry_at == moment + timedelta(minutes=1)
    assert notification.last_error is not None


async def test_failed_after_all_attempts(db_session: AsyncSession) -> None:
    """1 → 5 → 30 минут, после третьей попытки статус failed."""
    notification = await _one_pending(db_session, channel="email")
    channel = FailingChannel(TransientChannelError("SMTP недоступен"))
    moment = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)

    for attempt, delay in enumerate(DELAYS, start=1):
        await deliver_batch(
            db_session,
            channels={"email": channel},
            contacts=LocalContactResolver(db_session),
            now=moment + timedelta(hours=attempt),
            delays=DELAYS,
        )
        await db_session.refresh(notification)
        assert notification.attempts == attempt
        assert notification.status == "pending"
        assert notification.next_retry_at == moment + timedelta(hours=attempt, minutes=delay)

    await deliver_batch(
        db_session,
        channels={"email": channel},
        contacts=LocalContactResolver(db_session),
        now=moment + timedelta(days=1),
        delays=DELAYS,
    )

    await db_session.refresh(notification)
    assert notification.status == "failed"
    assert notification.attempts == 4
    assert notification.next_retry_at is None


async def test_permanent_failure_is_not_retried(db_session: AsyncSession) -> None:
    notification = await _one_pending(db_session, channel="email")
    channel = FailingChannel(PermanentChannelError("SMTP отверг сообщение"))

    await deliver_batch(
        db_session,
        channels={"email": channel},
        contacts=LocalContactResolver(db_session),
        delays=DELAYS,
    )

    await db_session.refresh(notification)
    assert notification.status == "failed"
    assert notification.attempts == 1
    assert notification.next_retry_at is None


async def test_retry_is_not_picked_up_before_its_time(db_session: AsyncSession) -> None:
    await _one_pending(db_session, channel="email")
    channel = FailingChannel(TransientChannelError("недоступен"))
    moment = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)

    await deliver_batch(
        db_session,
        channels={"email": channel},
        contacts=LocalContactResolver(db_session),
        now=moment,
        delays=DELAYS,
    )
    result = await deliver_batch(
        db_session,
        channels={"email": channel},
        contacts=LocalContactResolver(db_session),
        now=moment + timedelta(seconds=30),
        delays=DELAYS,
    )

    assert result.size == 0
    assert channel.attempts == 1


async def test_unknown_channel_fails_permanently(db_session: AsyncSession) -> None:
    notification = await _one_pending(db_session, channel="trueconf")

    await deliver_batch(
        db_session,
        channels={"web": WebChannel()},
        contacts=LocalContactResolver(db_session),
        delays=DELAYS,
    )

    await db_session.refresh(notification)
    assert notification.status == "failed"
    assert notification.last_error is not None
    assert "trueconf" in notification.last_error


async def test_address_is_resolved_and_not_stored(db_session: AsyncSession) -> None:
    """Адрес доставки не должен попадать в БД (FR-DLV-4)."""
    user = await make_user(db_session, "duty")
    db_session.add(
        UserContact(user_id=user.id, channel="email", address="duty@example.local")
    )
    topic = await make_topic(db_session)
    await subscribe(db_session, user, topic, channels=["email"])
    await make_incident(db_session)
    await route_batch(db_session)

    channel = RecordingChannel()
    channel.name = "email"
    await deliver_batch(
        db_session,
        channels={"email": channel},
        contacts=LocalContactResolver(db_session),
        delays=DELAYS,
    )

    assert channel.sent[0][1] == "duty@example.local"  # адрес дошёл до канала

    notification = (await db_session.execute(select(Notification))).scalar_one()
    stored = {
        column.name: getattr(notification, column.name)
        for column in Notification.__table__.columns
    }
    assert not any(
        "duty@example.local" in str(value) for value in stored.values()
    )  # но в строке его нет


async def test_missing_address_fails_permanently(db_session: AsyncSession) -> None:
    """Контакта нет — повторять бессмысленно, это проблема данных."""
    from app.adapters.channels import EmailChannel, SmtpSettings

    notification = await _one_pending(db_session, channel="email")
    channel = EmailChannel(
        SmtpSettings(host="smtp.invalid", port=25, username="", password="", sender="a@b.c")
    )

    await deliver_batch(
        db_session,
        channels={"email": channel},
        contacts=LocalContactResolver(db_session),
        delays=DELAYS,
    )

    await db_session.refresh(notification)
    assert notification.status == "failed"
    assert notification.attempts == 1


def test_retry_schedule() -> None:
    now = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)

    assert next_retry_at(1, now=now, delays=DELAYS) == now + timedelta(minutes=1)
    assert next_retry_at(2, now=now, delays=DELAYS) == now + timedelta(minutes=5)
    assert next_retry_at(3, now=now, delays=DELAYS) == now + timedelta(minutes=30)
    assert next_retry_at(4, now=now, delays=DELAYS) is None


async def test_two_workers_do_not_duplicate_delivery(database_url: str) -> None:
    """SKIP LOCKED: два воркера на одной очереди не отправят дважды.

    Тест работает вне общей откатываемой транзакции: конкурентность можно
    проверить только на реально закоммиченных данных, которые видны второму
    соединению. Поэтому он же за собой и убирает.
    """
    engine = create_async_engine(database_url)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    channels = {"first": RecordingChannel(), "second": RecordingChannel()}

    async def worker(name: str) -> int:
        async with maker() as session, session.begin():
            result = await deliver_batch(
                session,
                channels={"web": channels[name]},
                contacts=LocalContactResolver(session),
                limit=10,
                delays=DELAYS,
            )
            return result.sent

    try:
        async with maker() as session, session.begin():
            await _truncate(session)
            topic = await make_topic(session, match_rule={})
            for index in range(6):
                user = await make_user(session, f"duty{index}")
                await subscribe(session, user, topic)
            await make_incident(session)
            await route_batch(session)

        first, second = await asyncio.gather(worker("first"), worker("second"))

        assert first + second == 6  # каждое уведомление обработано ровно один раз
        delivered = [item[0] for item in channels["first"].sent + channels["second"].sent]
        assert len(delivered) == len(set(delivered))  # дублей нет

        async with maker() as session:
            statuses = (await session.execute(select(Notification.status))).scalars().all()
        assert set(statuses) == {"sent"}
    finally:
        async with maker() as session, session.begin():
            await _truncate(session)
        await engine.dispose()


async def _truncate(session: AsyncSession) -> None:
    await session.execute(
        text(
            "TRUNCATE notifications, incidents, monitored_objects, subscriptions, "
            "topics, user_roles, users RESTART IDENTITY CASCADE"
        )
    )
