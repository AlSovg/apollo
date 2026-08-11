"""Демонстрационное состояние стенда.

    python -m app.demo_seed            # привести стенд в состояние «есть что показать»
    python -m app.demo_seed --reset    # убрать демо-инциденты и демо-пользователей

`app.seed` заводит темы и подписки, но намеренно не трогает события — это
техдолг был правильным решением на шаге сборки, где ещё не было конвейера
нормализации, но после первого `docker compose up` он превращается в пустой
инбокс без единого уведомления. Эта команда закрывает разрыв: заводит четырёх
локальных пользователей с разными ролями, контактами и каналами (заглушка на
случай, если LDAP ещё не поднят — та же логика, что у `python -m app.cli
create-admin`) и прогоняет набор инцидентов Zabbix/SolarWinds разной давности
и критичности.

Данные подобраны под каталог тем из `app.seed`: на каждое поле правила отбора
есть и попадающий инцидент, и непопадающий. Тема без совпадений или инцидент,
не попавший никуда, кроме «Все события», — это не пробел в данных, а показ
того, ради чего темы и нужны.

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
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from zoneinfo import ZoneInfo

from sqlalchemy import CursorResult, delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.identity import hash_password, ldap_settings
from app.adapters.identity.base import DirectoryUnavailableError
from app.adapters.identity.ldap import LdapIdentityProvider
from app.config import get_settings
from app.db import get_engine, get_sessionmaker
from app.models.events import Incident, RawEvent
from app.models.identity import User, UserContact, UserRole
from app.models.subscriptions import Subscription, Topic
from app.seed import _seed as seed_topics_and_roles
from app.services.normalizer import process_batch
from app.services.partitions import ensure_partitions
from app.services.router import route_batch
from app.services.users import map_groups_to_roles, sync_roles, upsert_user

_PASSWORD_ALPHABET = string.ascii_letters + string.digits
_MOSCOW = ZoneInfo("Europe/Moscow")


@dataclass(frozen=True)
class DemoUser:
    """Демо-пользователь: роль, контакты, добровольные подписки.

    Обязательные подписки здесь не перечислены — они вычисляются из роли
    (topic_roles в app.seed) и всегда идут в канал web: так задана вьюха
    v_effective_subscriptions. Email и TrueConf появляются только через
    добровольную подписку, поэтому каналы указаны именно тут.
    """

    username: str
    role: str
    # channel → адрес. Ложится в user_contacts — единственную таблицу с ПДн
    # (раздел 5 AGENTS.md), очищается при переходе на LDAP.
    contacts: dict[str, str]
    # код темы → каналы доставки
    subscriptions: dict[str, list[str]]


# Логины не пересекаются с breakglass из app.cli create-admin. Набор подобран
# так, чтобы на демо было видно разницу: два инженера с обязательными
# подписками и разными каналами, наблюдатель сети и владелец веб-сервиса.
DEMO_USERS: list[DemoUser] = [
    DemoUser(
        username="duty.engineer",
        role="engineer",
        contacts={"email": "duty.engineer@apollo.test", "trueconf": "duty.engineer"},
        # обязательные по роли engineer: prod-db, critical-any, scada-asu
        subscriptions={"all-events": ["web"], "web-prod": ["web", "email"]},
    ),
    DemoUser(
        username="shift.lead",
        role="engineer",
        contacts={"email": "shift.lead@apollo.test", "trueconf": "shift.lead"},
        # TrueConf — на старшего смены: канал обязателен по условиям кейса,
        # и на демо он должен на ком-то реально сработать
        subscriptions={"network-core": ["web", "trueconf"], "sla-watch": ["web", "email"]},
    ),
    DemoUser(
        username="noc.viewer",
        role="viewer",
        contacts={"email": "noc.viewer@apollo.test"},
        # обязательная по роли viewer: network-core
        subscriptions={"all-events": ["web"]},
    ),
    DemoUser(
        username="service.owner",
        role="viewer",
        contacts={},
        # владелец сервиса подписан узко — только своя группа объектов
        subscriptions={"web-prod": ["web"]},
    ),
]


@dataclass(frozen=True)
class DirectoryUser:
    """Доменная учётка, которой стенд назначает подписку заранее.

    При AUTH_PROVIDER=ldap каналы email и TrueConf работают только у доменных
    учёток: адрес доставки резолвится из каталога, а локальных демо-
    пользователей там нет. Обязательные подписки по роли идут в web, поэтому
    без строки в subscriptions доставка в TrueConf на чистом стенде молчит,
    пока кто-нибудь не подпишется руками.

    Происхождение — assigned: так это и выглядит в бою, когда администратор
    назначает канал сотруднику. Отписаться пользователь не может, но каналы
    поменять — да.
    """

    username: str
    # код темы → каналы доставки
    subscriptions: dict[str, list[str]]


# Логины совпадают с deploy/ad/bootstrap-users.sh. Каждой учётке там
# проставлен свой TrueConf ID (ipPhone) и почта, поэтому сообщения разных
# дежурных приходят в разные чаты, а не в один общий.
DIRECTORY_USERS: list[DirectoryUser] = [
    # Дежурный: критичное с тегами sla+prod — в TrueConf
    DirectoryUser("ad.duty", {"sla-watch": ["web", "trueconf"]}),
    # Второй инженер: сетевой контур, тоже TrueConf — видно, что канал
    # работает не на одном избранном пользователе
    DirectoryUser("ad.eng", {"network-core": ["web", "trueconf"]}),
    # Наблюдатель: почта. Адрес резолвится из каталога; уйдёт ли письмо,
    # зависит от SMTP_HOST, и это видно на дашборде доставки
    DirectoryUser("ad.viewer", {"web-prod": ["web", "email"]}),
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
    tags: list[str] | None = None,
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
        # Теги решают, попадёт ли событие в темы с tags_any / tags_all
        "tags": tags if tags is not None else ["demo"],
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
    group: str,
) -> dict[str, Any]:
    moment = _ago(minutes_ago)
    return {
        "name": name,
        "severity": severity_label,
        # Плоский ключ group адаптер кладёт в object_group — по нему работают
        # темы с object_groups
        "group": group,
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

    Набор подобран под каталог тем из app.seed: на каждое поле MatchRule есть
    инцидент, который под него попадает, и инцидент, который не попадает.
    Тема без единого совпадения на демо выглядит как сломанная маршрутизация.
    """
    events: list[tuple[str, dict[str, Any]]] = []

    # --- Zabbix -------------------------------------------------------------

    # A: Disaster, только что открылся — самый свежий и самый громкий инцидент.
    #    Теги scada + группа Приобье → тема scada-asu (object_groups + tags_any)
    events.append((
        "zabbix",
        _zabbix_payload(
            event_id="demo-901", host="gpn-scada-07", hostgroup="АСУ ТП / Приобье",
            trigger_id="90101", trigger_name="Недоступен узел",
            nseverity="5", severity_label="Disaster",
            resolved=False, minutes_ago=6, value="0",
            tags=["demo", "scada", "prod"],
        ),
    ))

    # B: High, открыт почти час — на грани "незамеченного простоя" из AGENTS.md.
    #    Теги sla + prod → единственная пара, дающая совпадение по tags_all
    events.append((
        "zabbix",
        _zabbix_payload(
            event_id="demo-902", host="gpn-db-prod-02", hostgroup="БД prod",
            trigger_id="90102", trigger_name="Диск заполнен на 94%",
            nseverity="4", severity_label="High",
            resolved=False, minutes_ago=47, value="94",
            tags=["demo", "sla", "prod"],
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
                tags=["demo", "prod"],
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
            tags=["demo", "prod"],
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

    # E: High на второй площадке АСУ ТП, три повтора за полтора часа — второй
    #    случай дедупликации, на этот раз на критичном инциденте
    for minutes_ago, value in ((95, "78"), (70, "83"), (33, "88"), (11, "92")):
        events.append((
            "zabbix",
            _zabbix_payload(
                event_id="demo-905", host="gpn-scada-11", hostgroup="АСУ ТП / Приобье",
                trigger_id="90105", trigger_name="Температура выше нормы",
                nseverity="4", severity_label="High",
                resolved=False, minutes_ago=minutes_ago, value=value,
                tags=["demo", "asu-tp", "sla", "prod"],
            ),
        ))

    # F: Average по веб-группе — попадает и в web-prod, и в sla-watch
    events.append((
        "zabbix",
        _zabbix_payload(
            event_id="demo-906", host="gpn-web-02", hostgroup="Веб prod",
            trigger_id="90106", trigger_name="Ошибки 5xx выше порога",
            nseverity="3", severity_label="Average",
            resolved=False, minutes_ago=15, value="4.7",
            tags=["demo", "sla", "prod"],
        ),
    ))

    # G: Disaster, закрытый пять часов назад — на дашборде даёт непустой MTTR
    events.append((
        "zabbix",
        _zabbix_payload(
            event_id="demo-907", host="gpn-db-prod-05", hostgroup="БД prod",
            trigger_id="90107", trigger_name="Репликация остановлена",
            nseverity="5", severity_label="Disaster",
            resolved=False, minutes_ago=5 * 60, value="0",
            tags=["demo", "sla", "prod"],
        ),
    ))
    events.append((
        "zabbix",
        _zabbix_payload(
            event_id="demo-907", host="gpn-db-prod-05", hostgroup="БД prod",
            trigger_id="90107", trigger_name="Репликация остановлена",
            nseverity="5", severity_label="Disaster",
            resolved=True, minutes_ago=5 * 60 - 38, value="1",
        ),
    ))

    # H: Information на стенде — тот самый шум, ради которого и нужны темы.
    #    Не попадает никуда, кроме all-events: дежурный его не увидит
    events.append((
        "zabbix",
        _zabbix_payload(
            event_id="demo-908", host="gpn-app-09", hostgroup="Прочее / stage",
            trigger_id="90108", trigger_name="Перезапуск службы",
            nseverity="1", severity_label="Information",
            resolved=False, minutes_ago=3 * 60, value="1",
            tags=["demo", "stage"],
        ),
    ))

    # --- SolarWinds ---------------------------------------------------------

    # I: Critical, узел сети недоступен — открыт 12 минут
    events.append((
        "solarwinds",
        _solarwinds_payload(
            name="Node Down", node="gpn-net-msk-14", ip="10.30.14.1",
            severity_label="Critical", resolved=False, minutes_ago=12, cpu="0",
            group="Сеть / Магистраль",
        ),
    ))

    # J: Serious, высокая загрузка — открыт больше часа
    events.append((
        "solarwinds",
        _solarwinds_payload(
            name="High CPU Utilization", node="gpn-net-spb-03", ip="10.30.3.10",
            severity_label="Serious", resolved=False, minutes_ago=85, cpu="91",
            group="Сеть / Магистраль",
        ),
    ))

    # K: Warning, закрыт позавчера — тоже с лентой problem + resolved
    events.append((
        "solarwinds",
        _solarwinds_payload(
            name="Interface Errors", node="gpn-net-tmn-02", ip="10.30.2.5",
            severity_label="Warning", resolved=False, minutes_ago=2 * 24 * 60, cpu="0",
            group="Сеть / Регионы",
        ),
    ))
    events.append((
        "solarwinds",
        _solarwinds_payload(
            name="Interface Errors", node="gpn-net-tmn-02", ip="10.30.2.5",
            severity_label="Warning", resolved=True, minutes_ago=2 * 24 * 60 - 35, cpu="0",
            group="Сеть / Регионы",
        ),
    ))

    # L: Serious с двумя повторами — дедупликация работает и на втором источнике
    for minutes_ago in (40, 18):
        events.append((
            "solarwinds",
            _solarwinds_payload(
                name="Packet Loss", node="gpn-net-msk-02", ip="10.30.2.1",
                severity_label="Serious", resolved=False, minutes_ago=minutes_ago, cpu="12",
                group="Сеть / Магистраль",
            ),
        ))

    # M: балансировщик в группе «Веб prod» — тема web-prod задана без sources,
    #    поэтому ловит объект из SolarWinds наравне с Zabbix
    events.append((
        "solarwinds",
        _solarwinds_payload(
            name="Response Time High", node="gpn-web-lb-01", ip="10.20.0.9",
            severity_label="Warning", resolved=False, minutes_ago=28, cpu="44",
            group="Веб prod",
        ),
    ))

    # N: Critical, закрытый девять часов назад — MTTR по сетевому контуру
    events.append((
        "solarwinds",
        _solarwinds_payload(
            name="Node Down", node="gpn-net-nsk-07", ip="10.30.7.1",
            severity_label="Critical", resolved=False, minutes_ago=9 * 60, cpu="0",
            group="Сеть / Регионы",
        ),
    ))
    events.append((
        "solarwinds",
        _solarwinds_payload(
            name="Node Down", node="gpn-net-nsk-07", ip="10.30.7.1",
            severity_label="Critical", resolved=True, minutes_ago=9 * 60 - 52, cpu="3",
            group="Сеть / Регионы",
        ),
    ))

    # O: тот же узел, что в I, но другой алерт — проверяет, что external_key
    #    склеен из узла И имени правила, иначе инциденты затёрли бы друг друга.
    #    Notice ниже порога network-core, поэтому уведомлений не породит
    events.append((
        "solarwinds",
        _solarwinds_payload(
            name="Interface Down", node="gpn-net-msk-14", ip="10.30.14.1",
            severity_label="Notice", resolved=False, minutes_ago=55, cpu="0",
            group="Сеть / Магистраль",
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


async def _seed_users(session: AsyncSession, password: str | None = None) -> list[str]:
    """Локальные демо-пользователи с ролями. Пароль печатается только при
    первом создании — реген при каждом запуске сломал бы уже настроенное
    для показа демо (пользователь мог войти заранее и что-то себе подписать).

    `password` задаёт всем один известный пароль вместо случайного. Нужен
    там, где креды должны совпасть с написанным в README, — на разборном
    стенде и при передаче кода на проверку. В бою так не делают.
    """
    printed: list[str] = []
    for demo in DEMO_USERS:
        existing = (
            await session.execute(select(User).where(User.username == demo.username))
        ).scalar_one_or_none()

        if existing is None:
            secret = password or "".join(
                secrets.choice(_PASSWORD_ALPHABET) for _ in range(20)
            )
            user = User(
                subject_id=f"local:{demo.username}",
                username=demo.username,
                provider="local",
                password_hash=hash_password(secret),
                is_active=True,
            )
            session.add(user)
            await session.flush()
            user_id = user.id
            printed.append(f"{demo.username:14} роль={demo.role:10} пароль={secret}")
        else:
            user_id = existing.id

        await session.execute(
            insert(UserRole)
            .values(user_id=user_id, role_code=demo.role, source="local")
            .on_conflict_do_nothing()
        )

        # Без контакта доставка в email/TrueConf останется без адреса и уйдёт
        # в ошибку — на демо это выглядит как неработающий канал
        for channel, address in demo.contacts.items():
            await session.execute(
                insert(UserContact)
                .values(user_id=user_id, channel=channel, address=address)
                .on_conflict_do_nothing(constraint="user_contacts_pk")
            )

    await session.flush()
    return printed


async def _seed_subscriptions(session: AsyncSession) -> int:
    """Добровольные подписки демо-пользователей.

    Обязательные подписки не заводятся строками — они вычисляются из ролей
    (v_effective_subscriptions) и всегда идут в web. Всё, что здесь, — это
    origin='self': то, что пользователь мог бы включить себе сам, включая
    выбор каналов email и trueconf.
    """
    topic_ids: dict[str, int] = {
        row.code: row.id
        for row in (await session.execute(select(Topic.code, Topic.id))).all()
    }

    created = 0
    for demo in DEMO_USERS:
        user_id = (
            await session.execute(select(User.id).where(User.username == demo.username))
        ).scalar_one()
        for topic_code, channels in demo.subscriptions.items():
            topic_id = topic_ids.get(topic_code)
            if topic_id is None:  # тему удалили руками — не падаем на демо
                print(f"  внимание: темы '{topic_code}' нет, подписка пропущена")
                continue
            inserted = (
                await session.execute(
                    insert(Subscription)
                    .values(
                        user_id=user_id,
                        topic_id=topic_id,
                        origin="self",
                        channels=channels,
                    )
                    .on_conflict_do_nothing(constraint="subscriptions_uq")
                    .returning(Subscription.id)
                )
            ).scalar_one_or_none()
            if inserted is not None:
                created += 1

    await session.flush()
    return created


async def _seed_directory_subscriptions(session: AsyncSession) -> tuple[int, str]:
    """Назначенные подписки доменных учёток. Возвращает (сколько, пояснение).

    Ветка целиком про AUTH_PROVIDER=ldap: при локальном входе доменных учёток
    нет и назначать нечего. Пользователь заводится здесь же, если он ещё ни
    разу не входил, — иначе назначать подписку было бы некому: запись в
    платформе создаёт вход, а стенд готовится раньше.
    """
    settings = get_settings()
    if settings.auth_provider != "ldap":
        return 0, "AUTH_PROVIDER=local — доменных подписок не требуется"

    # Назначенной подписке нужен тот, кто её назначил (subscriptions_granted_chk)
    granted_by = (
        await session.execute(
            select(User.id)
            .join(UserRole, UserRole.user_id == User.id)
            .where(UserRole.role_code == "admin", User.is_active)
            .order_by(User.id)
            .limit(1)
        )
    ).scalar_one_or_none()
    if granted_by is None:
        return 0, "нет ни одного администратора — сначала `app.cli create-admin`"

    provider = LdapIdentityProvider(ldap_settings(settings))
    topic_ids: dict[str, int] = {
        row.code: row.id
        for row in (await session.execute(select(Topic.code, Topic.id))).all()
    }

    created = 0
    missing: list[str] = []
    for directory_user in DIRECTORY_USERS:
        user = (
            await session.execute(
                select(User).where(
                    User.username == directory_user.username, User.provider == "ldap"
                )
            )
        ).scalar_one_or_none()

        if user is None:
            try:
                subject = await provider.describe_by_username(directory_user.username)
            except DirectoryUnavailableError:
                return created, "каталог недоступен — доменные подписки пропущены"
            if subject is None:
                missing.append(directory_user.username)
                continue
            user = await upsert_user(session, subject, "ldap")
            # Роли приезжают из групп каталога — тем же путём, что и при входе
            await sync_roles(session, user, await map_groups_to_roles(session, subject.roles))

        for topic_code, channels in directory_user.subscriptions.items():
            topic_id = topic_ids.get(topic_code)
            if topic_id is None:
                print(f"  внимание: темы '{topic_code}' нет, подписка пропущена")
                continue
            inserted = (
                await session.execute(
                    insert(Subscription)
                    .values(
                        user_id=user.id,
                        topic_id=topic_id,
                        origin="assigned",
                        channels=channels,
                        granted_by=granted_by,
                    )
                    .on_conflict_do_nothing(constraint="subscriptions_uq")
                    .returning(Subscription.id)
                )
            ).scalar_one_or_none()
            if inserted is not None:
                created += 1

    await session.flush()
    note = f"нет в каталоге: {', '.join(missing)}" if missing else "каталог: все учётки найдены"
    return created, note


_DEMO_EXTERNAL_KEYS: dict[str, list[str]] = {
    "zabbix": [f"demo-{number}" for number in range(901, 909)],
    "solarwinds": [
        "gpn-net-msk-14|Node Down",
        "gpn-net-msk-14|Interface Down",
        "gpn-net-spb-03|High CPU Utilization",
        "gpn-net-tmn-02|Interface Errors",
        "gpn-net-msk-02|Packet Loss",
        "gpn-web-lb-01|Response Time High",
        "gpn-net-nsk-07|Node Down",
    ],
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

    # Контакты, роли и подписки уезжают каскадом за пользователем
    usernames = [demo.username for demo in DEMO_USERS]
    # execute() типизирован как Result, счётчик строк есть только у CursorResult
    deleted: CursorResult[Any] = cast(
        "CursorResult[Any]",
        await session.execute(delete(User).where(User.username.in_(usernames))),
    )
    removed_users = deleted.rowcount

    await session.flush()
    print(f"Удалено демо-инцидентов: {removed_incidents}, демо-пользователей: {removed_users}")


async def _main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="app.demo_seed")
    parser.add_argument(
        "--reset", action="store_true", help="Сначала убрать демо-инциденты и демо-пользователей"
    )
    parser.add_argument(
        "--password",
        help="Один известный пароль всем демо-учёткам вместо случайного "
        "(разборный стенд, передача кода на проверку)",
    )
    args = parser.parse_args(argv)

    await ensure_partitions(1)

    try:
        async with get_sessionmaker()() as session:
            if args.reset:
                await _reset(session)
                await session.commit()

            await seed_topics_and_roles(session)
            printed_users = await _seed_users(session, args.password)
            subscriptions = await _seed_subscriptions(session)
            assigned, directory_note = await _seed_directory_subscriptions(session)
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

    print(f"Новых raw_events: {inserted}, добровольных подписок: {subscriptions}")
    print(f"Назначенных подписок доменным учёткам: {assigned} ({directory_note})")
    print(f"Обработано нормализатором: {normalized.processed}, ошибок: {normalized.failed}")
    print(f"Инцидентов смаршрутизировано: {routed.size}, уведомлений создано: {routed.created}")
    if printed_users:
        print("\nНовые демо-пользователи (пароль показан один раз):")
        for line in printed_users:
            print(f"  {line}")
    print("\nГотово. Инбокс — http://localhost, войти под любым из демо-пользователей.")
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(_main(sys.argv[1:])))


if __name__ == "__main__":
    main()
