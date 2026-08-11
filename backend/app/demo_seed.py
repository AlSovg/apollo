"""Демонстрационное состояние стенда.

    python -m app.demo_seed            # привести стенд в состояние «есть что показать»
    python -m app.demo_seed --reset    # убрать демо-инциденты и демо-пользователей

`app.seed` заводит темы и подписки, но намеренно не трогает события — это
техдолг был правильным решением на шаге сборки, где ещё не было конвейера
нормализации, но после первого `docker compose up` он превращается в пустой
инбокс без единого уведомления. Эта команда закрывает разрыв: заводит два
локальных пользователя с разными ролями (заглушка на случай, если LDAP ещё
не поднят — та же логика, что у `python -m app.cli create-admin`) и прогоняет
набор инцидентов Zabbix/SolarWinds разной давности и критичности.

Инциденты заводятся НЕ прямой записью в incidents/notifications, а как
настоящие raw_events, пропущенные через `process_batch`/`route_batch` —
те же функции, что крутят воркеры normalizer и router. Так демо проверяет
ту же логику, что показывает, а не отдельную копию для красоты.

Идемпотентность: external_key событий фиксирован, поэтому повторный запуск
не плодит новые инциденты — `process_batch` лишь обновит dedup_count и
last_event_at через тот же ON CONFLICT, что и в бою. Уже смаршрутизированные
инциденты (last_notified_at не пуст) `route_batch` повторно не трогает, так
что уведомления тоже не дублируются. raw_events при этом накапливаются, как
и в бою при реальных ретраях источника — это append-only слой, его нарочно
не чистит даже `--reset` (правило 2 из AGENTS.md).
"""

import argparse
import asyncio
import hashlib
import json
import secrets
import string
import sys
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.identity import hash_password
from app.db import get_engine, get_sessionmaker
from app.models.events import Incident, RawEvent
from app.models.identity import User, UserRole
from app.models.subscriptions import Subscription, Topic
from app.seed import _seed as seed_topics_and_roles
from app.services.normalizer import process_batch
from app.services.partitions import ensure_partitions
from app.services.router import route_batch

_PASSWORD_ALPHABET = string.ascii_letters + string.digits
_MOSCOW = ZoneInfo("Europe/Moscow")

# Логины не пересекаются с breakglass из app.cli create-admin. Роль engineer
# ловит обязательную подписку на тему "prod-db" из app.seed, viewer — нет,
# поэтому обоих ещё подписываем добровольно на "all-events" ниже.
DEMO_USERS: list[tuple[str, str]] = [
    ("duty.engineer", "engineer"),
    ("noc.viewer", "viewer"),
]


def _ago(minutes: float) -> datetime:
    """Локальное время сервера Zabbix/SolarWinds на стенде — без таймзоны в payload."""
    return (datetime.now(tz=_MOSCOW) - timedelta(minutes=minutes)).replace(tzinfo=None)


def _zabbix_payload(
    *,
    event_id: str,
    host: str,
    hostgroup: str,
    trigger_id: str,
    trigger_name: str,
    nseverity: str,
    severity_label: str,
    resolved: bool,
    minutes_ago: float,
    value: str,
) -> dict[str, Any]:
    moment = _ago(minutes_ago)
    return {
        "event_id": event_id,
        "host": host,
        "host_technical_name": host,
        "host_ip": "*UNKNOWN*",
        "hostgroup": hostgroup,
        "trigger_id": trigger_id,
        "trigger_name": trigger_name,
        "trigger_expression": f"last(/{host}/demo.metric)>{value}",
        "severity": severity_label,
        "event_nseverity": nseverity,
        "status": "RESOLVED" if resolved else "PROBLEM",
        "event_value": "0" if resolved else "1",
        "timestamp": moment.strftime("%Y.%m.%d %H:%M:%S"),
        "date": moment.strftime("%Y.%m.%d"),
        "time": moment.strftime("%H:%M:%S"),
        "item_lastvalue": value,
        "tags": ["demo"],
        "message": f"{'Resolved' if resolved else 'Problem'}: {trigger_name}\nHost: {host}",
    }


def _solarwinds_payload(
    *,
    name: str,
    node: str,
    ip: str,
    severity_label: str,
    resolved: bool,
    minutes_ago: float,
    cpu: str,
) -> dict[str, Any]:
    moment = _ago(minutes_ago)
    return {
        "name": name,
        "severity": severity_label,
        # Формат reset-события не подтверждён на боевом стенде (открытый вопрос
        # 1 в docs/tz.md) — используется та же гипотеза, что в tools/load_generator.py
        "status": "Reset" if resolved else "Triggered",
        "node": node,
        "ip": ip,
        "cpu": cpu,
        "time": moment.strftime("%m/%d/%Y %I:%M:%S %p"),
    }


def _payload_hash(payload: dict[str, Any]) -> bytes:
    body = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
    return hashlib.sha256(body).digest()


def _demo_raw_events() -> list[tuple[str, dict[str, Any]]]:
    """(source, payload) в порядке отправки. Разная давность и критичность —
    приёмка шага 14 build-prompts.md: "набор инцидентов разной давности и severity".
    """
    events: list[tuple[str, dict[str, Any]]] = []

    # A: Disaster, только что открылся — самый свежий и самый громкий инцидент
    events.append((
        "zabbix",
        _zabbix_payload(
            event_id="demo-901", host="gpn-scada-07", hostgroup="АСУ ТП / Приобье",
            trigger_id="90101", trigger_name="Недоступен узел",
            nseverity="5", severity_label="Disaster",
            resolved=False, minutes_ago=6, value="0",
        ),
    ))

    # B: High, открыт почти час — на грани "незамеченного простоя" из AGENTS.md
    events.append((
        "zabbix",
        _zabbix_payload(
            event_id="demo-902", host="gpn-db-prod-02", hostgroup="БД prod",
            trigger_id="90102", trigger_name="Диск заполнен на 94%",
            nseverity="4", severity_label="High",
            resolved=False, minutes_ago=47, value="94",
        ),
    ))

    # C: Average, один и тот же external_key прилетает четыре раза за три часа —
    # демонстрирует дедупликацию и счётчик повторов на дашборде
    for minutes_ago, value in ((180, "91"), (130, "93"), (65, "95"), (20, "97")):
        events.append((
            "zabbix",
            _zabbix_payload(
                event_id="demo-903", host="gpn-app-03", hostgroup="БД prod",
                trigger_id="90103", trigger_name="Высокая загрузка CPU",
                nseverity="3", severity_label="Average",
                resolved=False, minutes_ago=minutes_ago, value=value,
            ),
        ))

    # D: Warning, уже закрыт вчера — problem + resolved, чтобы в карточке была лента
    events.append((
        "zabbix",
        _zabbix_payload(
            event_id="demo-904", host="gpn-web-01", hostgroup="Веб prod",
            trigger_id="90104", trigger_name="Растёт время отклика",
            nseverity="2", severity_label="Warning",
            resolved=False, minutes_ago=26 * 60, value="1200",
        ),
    ))
    events.append((
        "zabbix",
        _zabbix_payload(
            event_id="demo-904", host="gpn-web-01", hostgroup="Веб prod",
            trigger_id="90104", trigger_name="Растёт время отклика",
            nseverity="2", severity_label="Warning",
            resolved=True, minutes_ago=26 * 60 - 20, value="180",
        ),
    ))

    # E: Critical (SolarWinds), узел сети недоступен — открыт 12 минут
    events.append((
        "solarwinds",
        _solarwinds_payload(
            name="Node Down", node="gpn-net-msk-14", ip="10.30.14.1",
            severity_label="Critical", resolved=False, minutes_ago=12, cpu="0",
        ),
    ))

    # F: Serious (SolarWinds), высокая загрузка — открыт больше часа
    events.append((
        "solarwinds",
        _solarwinds_payload(
            name="High CPU Utilization", node="gpn-net-spb-03", ip="10.30.3.10",
            severity_label="Serious", resolved=False, minutes_ago=85, cpu="91",
        ),
    ))

    # G: Warning (SolarWinds), закрыт позавчера — тоже с лентой problem + resolved
    events.append((
        "solarwinds",
        _solarwinds_payload(
            name="Interface Errors", node="gpn-net-tmn-02", ip="10.30.2.5",
            severity_label="Warning", resolved=False, minutes_ago=2 * 24 * 60, cpu="0",
        ),
    ))
    events.append((
        "solarwinds",
        _solarwinds_payload(
            name="Interface Errors", node="gpn-net-tmn-02", ip="10.30.2.5",
            severity_label="Warning", resolved=True, minutes_ago=2 * 24 * 60 - 35, cpu="0",
        ),
    ))

    return events


async def _insert_raw_events(session: AsyncSession) -> int:
    inserted = 0
    for source, payload in _demo_raw_events():
        statement = (
            insert(RawEvent)
            .values(source=source, payload=payload, payload_hash=_payload_hash(payload))
            .on_conflict_do_nothing(constraint="raw_events_dedup_uq")
            .returning(RawEvent.id)
        )
        if (await session.execute(statement)).scalar_one_or_none() is not None:
            inserted += 1
    await session.flush()
    return inserted


async def _seed_users(session: AsyncSession) -> list[str]:
    """Локальные демо-пользователи с ролями. Пароль печатается только при
    первом создании — реген при каждом запуске сломал бы уже настроенное
    для показа демо (пользователь мог войти заранее и что-то себе подписать).
    """
    printed: list[str] = []
    for username, role_code in DEMO_USERS:
        existing = (
            await session.execute(select(User).where(User.username == username))
        ).scalar_one_or_none()

        if existing is None:
            secret = "".join(secrets.choice(_PASSWORD_ALPHABET) for _ in range(20))
            user = User(
                subject_id=f"local:{username}",
                username=username,
                provider="local",
                password_hash=hash_password(secret),
                is_active=True,
            )
            session.add(user)
            await session.flush()
            user_id = user.id
            printed.append(f"{username:14} роль={role_code:10} пароль={secret}")
        else:
            user_id = existing.id

        await session.execute(
            insert(UserRole)
            .values(user_id=user_id, role_code=role_code, source="local")
            .on_conflict_do_nothing()
        )

    await session.flush()
    return printed


async def _subscribe_to_all_events(session: AsyncSession) -> None:
    """viewer не входит в mandatory_roles темы 'all-events' (см. app.seed) —
    без добровольной подписки его инбокс на демо останется пустым.
    """
    topic_id = (
        await session.execute(select(Topic.id).where(Topic.code == "all-events"))
    ).scalar_one()

    for username, _role in DEMO_USERS:
        user_id = (
            await session.execute(select(User.id).where(User.username == username))
        ).scalar_one()
        await session.execute(
            insert(Subscription)
            .values(user_id=user_id, topic_id=topic_id, origin="self", channels=["web"])
            .on_conflict_do_nothing(constraint="subscriptions_uq")
        )
    await session.flush()


_DEMO_EXTERNAL_KEYS: dict[str, list[str]] = {
    "zabbix": ["demo-901", "demo-902", "demo-903", "demo-904"],
    "solarwinds": ["gpn-net-msk-14|Node Down", "gpn-net-spb-03|High CPU Utilization",
                    "gpn-net-tmn-02|Interface Errors"],
}


async def _reset(session: AsyncSession) -> None:
    """Убрать демо-инциденты (каскадом — их notifications/incident_events) и
    демо-пользователей. raw_events НЕ трогаем: append-only, правило 2 из AGENTS.md —
    следующий запуск без --reset заведёт инциденты заново поверх них.
    """
    removed_incidents = 0
    for source, keys in _DEMO_EXTERNAL_KEYS.items():
        rows = (
            await session.execute(
                select(Incident).where(Incident.source == source, Incident.external_key.in_(keys))
            )
        ).scalars().all()
        for incident in rows:
            await session.delete(incident)
        removed_incidents += len(rows)

    usernames = [username for username, _role in DEMO_USERS]
    removed_users = (
        await session.execute(delete(User).where(User.username.in_(usernames)))
    ).rowcount

    await session.flush()
    print(f"Удалено демо-инцидентов: {removed_incidents}, демо-пользователей: {removed_users}")


async def _main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="app.demo_seed")
    parser.add_argument(
        "--reset", action="store_true", help="Сначала убрать демо-инциденты и демо-пользователей"
    )
    args = parser.parse_args(argv)

    await ensure_partitions(1)

    try:
        async with get_sessionmaker()() as session:
            if args.reset:
                await _reset(session)
                await session.commit()

            await seed_topics_and_roles(session)
            printed_users = await _seed_users(session)
            await _subscribe_to_all_events(session)
            await session.commit()

            inserted = await _insert_raw_events(session)
            await session.commit()

            now = datetime.now(tz=UTC)
            async with session.begin():
                normalized = await process_batch(session, limit=200, now=now)
            async with session.begin():
                routed = await route_batch(session, limit=200, now=now)
    finally:
        await get_engine().dispose()

    print(f"Новых raw_events: {inserted}")
    print(f"Обработано нормализатором: {normalized.processed}, ошибок: {normalized.failed}")
    print(f"Инцидентов смаршрутизировано: {routed.size}, уведомлений создано: {routed.created}")
    if printed_users:
        print("\nНовые демо-пользователи (пароль показан один раз):")
        for line in printed_users:
            print(f"  {line}")
    print("\nГотово. Инбокс — http://localhost, войти под duty.engineer или noc.viewer.")
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(_main(sys.argv[1:])))


if __name__ == "__main__":
    main()
