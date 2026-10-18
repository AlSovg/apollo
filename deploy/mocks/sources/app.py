"""Эмулятор систем-источников: Zabbix 7 и SolarWinds.

Подстановка ВМ №1 и №2 из AGENTS.md §3. На стендовых машинах по замыслу стоит
«генератор событий»; сюда он переехал в контейнер, потому что SSH ни на одну из
ВМ пока не проходит (см. `docs/integration-runbook.md` §4). Контракт от этого не
меняется: эмулятор шлёт ровно те же вебхуки на `POST /api/v1/ingest/{source}`,
что и боевые системы — те же поля, те же форматы времени, тот же способ
аутентификации. Переключение на настоящий источник = выключить этот сервис.

Два режима работают одновременно:

* **Поток** — фоновая задача раз в `SIM_INTERVAL_S` открывает новую проблему,
  повторяет уже открытую (дедупликация) или закрывает её. Темп низкий: стенд
  должен выглядеть живым, а не заваливать инбокс.
* **Сценарии** — `POST /scenario/{name}` проигрывает заранее собранный сюжет
  целиком. Это то, что показывают со сцены: нажал — через несколько секунд
  инцидент виден в инбоксе дежурного.

События идут как боевые (`is_test=false`): группы хостов настоящие, флага
`meta.testEvent` нет. Иначе маршрутизация и доставка не отработали бы (FR-INC-7),
и сквозной путь остался бы непроверенным.

Управление:
  SIM_TARGET_URL          база приёмника (по умолчанию http://backend:8000)
  INGEST_TOKEN_ZABBIX     X-Webhook-Token источника zabbix
  INGEST_TOKEN_SOLARWINDS X-Webhook-Token источника solarwinds
  INGEST_HMAC_ZABBIX      HMAC-секрет (hex) источника zabbix, если он задан
  INGEST_HMAC_SOLARWINDS  то же для solarwinds
  SIM_INTERVAL_S          пауза между событиями потока (25)
  SIM_DUP_RATIO         доля повторов уже открытой проблемы (0.45)
  SIM_RESOLVE_RATIO     доля закрытий среди повторов (0.3)
  SIM_AUTOSTART         запускать поток при старте (true)
  SIM_TZ                таймзона в payload — как у источника в реестре (Europe/Moscow)
"""

import asyncio
import hashlib
import hmac
import json
import logging
import os
import random
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal
from zoneinfo import ZoneInfo

import httpx
from fastapi import FastAPI
from fastapi.responses import JSONResponse

logging.basicConfig(
    level=os.environ.get("SIM_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("simulator")

TARGET_URL = os.environ.get("SIM_TARGET_URL", "http://backend:8000").rstrip("/")
INTERVAL_S = float(os.environ.get("SIM_INTERVAL_S", "25"))
DUP_RATIO = float(os.environ.get("SIM_DUP_RATIO", "0.45"))
RESOLVE_RATIO = float(os.environ.get("SIM_RESOLVE_RATIO", "0.3"))
AUTOSTART = os.environ.get("SIM_AUTOSTART", "true").lower() not in ("0", "false", "no")
TZ = ZoneInfo(os.environ.get("SIM_TZ", "Europe/Moscow"))

# Те же переменные, что применяет `python -m app.cli sync-tokens`: реестр
# источников и отправитель берут секрет из одного места, копировать нечего.
TOKENS: dict[str, str] = {
    "zabbix": os.environ.get("INGEST_TOKEN_ZABBIX", ""),
    "solarwinds": os.environ.get("INGEST_TOKEN_SOLARWINDS", ""),
}
HMAC_SECRETS: dict[str, str] = {
    "zabbix": os.environ.get("INGEST_HMAC_ZABBIX", ""),
    "solarwinds": os.environ.get("INGEST_HMAC_SOLARWINDS", ""),
}

Source = Literal["zabbix", "solarwinds"]

# --------------------------------------------------------------------------
# Парк объектов
#
# Совпадает с `backend/app/demo_seed.py` намеренно: эмулятор должен попадать в
# те же темы из `app.seed` и в те же `monitored_objects`, что и демо-данные.
# Разойдутся — события начнут создавать вторую, параллельную инфраструктуру, и
# на дашборде окажется вдвое больше объектов, чем в легенде стенда.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ZabbixRule:
    trigger_id: str
    name: str
    nseverity: str
    severity_label: str
    value_range: tuple[int, int]


@dataclass(frozen=True)
class ZabbixHost:
    host: str
    hostgroup: str
    tags: list[str]
    rules: list[ZabbixRule]


ZABBIX_FLEET: list[ZabbixHost] = [
    ZabbixHost(
        host="gpn-scada-07",
        hostgroup="АСУ ТП / Приобье",
        tags=["scada", "prod"],
        rules=[
            ZabbixRule("91001", "Недоступен узел", "5", "Disaster", (0, 0)),
            ZabbixRule("91002", "Потеря связи с контроллером", "4", "High", (0, 3)),
        ],
    ),
    ZabbixHost(
        host="gpn-scada-11",
        hostgroup="АСУ ТП / Приобье",
        tags=["asu-tp", "sla", "prod"],
        rules=[
            ZabbixRule("91003", "Температура выше нормы", "4", "High", (78, 96)),
            ZabbixRule("91004", "Давление вне уставки", "3", "Average", (12, 30)),
        ],
    ),
    ZabbixHost(
        host="gpn-db-prod-02",
        hostgroup="БД prod",
        tags=["sla", "prod"],
        rules=[
            ZabbixRule("91005", "Диск заполнен на 94%", "4", "High", (88, 99)),
            ZabbixRule("91006", "Долгие запросы к БД", "3", "Average", (5, 40)),
        ],
    ),
    ZabbixHost(
        host="gpn-db-prod-05",
        hostgroup="БД prod",
        tags=["sla", "prod"],
        rules=[
            ZabbixRule("91007", "Репликация остановлена", "5", "Disaster", (0, 0)),
        ],
    ),
    ZabbixHost(
        host="gpn-app-03",
        hostgroup="БД prod",
        tags=["prod"],
        rules=[
            ZabbixRule("91008", "Высокая загрузка CPU", "3", "Average", (85, 99)),
        ],
    ),
    ZabbixHost(
        host="gpn-web-01",
        hostgroup="Веб prod",
        tags=["prod"],
        rules=[
            ZabbixRule("91009", "Растёт время отклика", "2", "Warning", (700, 2400)),
        ],
    ),
    ZabbixHost(
        host="gpn-web-02",
        hostgroup="Веб prod",
        tags=["sla", "prod"],
        rules=[
            ZabbixRule("91010", "Ошибки 5xx выше порога", "3", "Average", (3, 12)),
        ],
    ),
    # Стенд-шум: Information не проходит ни в одну тему, кроме all-events.
    # Ради него темы и заведены — без шума нечего показывать на фильтрации.
    ZabbixHost(
        host="gpn-app-09",
        hostgroup="Прочее / stage",
        tags=["stage"],
        rules=[
            ZabbixRule("91011", "Перезапуск службы", "1", "Information", (1, 1)),
            ZabbixRule("91012", "Обновлён сертификат", "1", "Information", (1, 1)),
        ],
    ),
]


@dataclass(frozen=True)
class SolarWindsNode:
    node: str
    ip: str
    group: str
    alerts: list[tuple[str, str]]  # (имя алерта, severity)


SOLARWINDS_FLEET: list[SolarWindsNode] = [
    SolarWindsNode(
        node="gpn-net-msk-14",
        ip="10.30.14.1",
        group="Сеть / Магистраль",
        alerts=[("Node Down", "Critical"), ("Interface Down", "Notice")],
    ),
    SolarWindsNode(
        node="gpn-net-msk-02",
        ip="10.30.2.1",
        group="Сеть / Магистраль",
        alerts=[("Packet Loss", "Serious"), ("High CPU Utilization", "Serious")],
    ),
    SolarWindsNode(
        node="gpn-net-spb-03",
        ip="10.30.3.10",
        group="Сеть / Магистраль",
        alerts=[("High CPU Utilization", "Serious"), ("Memory Utilization", "Warning")],
    ),
    SolarWindsNode(
        node="gpn-net-tmn-02",
        ip="10.30.2.5",
        group="Сеть / Регионы",
        alerts=[("Interface Errors", "Warning"), ("Node Down", "Critical")],
    ),
    SolarWindsNode(
        node="gpn-net-nsk-07",
        ip="10.30.7.1",
        group="Сеть / Регионы",
        alerts=[("Node Down", "Critical"), ("Packet Loss", "Serious")],
    ),
    # Балансировщик в группе «Веб prod»: тема web-prod задана без sources и
    # обязана ловить объект из SolarWinds наравне с Zabbix
    SolarWindsNode(
        node="gpn-web-lb-01",
        ip="10.20.0.9",
        group="Веб prod",
        alerts=[("Response Time High", "Warning"), ("Node Down", "Critical")],
    ),
]

# Шесть узлов выше — постоянный состав сценариев, он совпадает с демо-данными.
# Потоку одного этого состава мало: ключ дедупликации SolarWinds — это
# «узел + имя алерта», и после того как все 12 пар превратились в инциденты,
# новых уведомлений источник больше не даёт (маршрутизатор берёт инцидент один
# раз, по `last_notified_at IS NULL`). Через полчаса стенд замолкал бы по
# SolarWinds, показывая только счётчики повторов. Поэтому поток берёт узлы из
# широкого пула — в реальной сети их сотни, а не шесть.
SOLARWINDS_SITES: list[tuple[str, str]] = [
    ("msk", "Сеть / Магистраль"),
    ("spb", "Сеть / Магистраль"),
    ("tmn", "Сеть / Регионы"),
    ("nsk", "Сеть / Регионы"),
]

SOLARWINDS_ALERTS: list[tuple[str, str]] = [
    ("Node Down", "Critical"),
    ("High CPU Utilization", "Serious"),
    ("Packet Loss", "Serious"),
    ("Interface Errors", "Warning"),
    ("Memory Utilization", "Warning"),
    ("Response Time High", "Warning"),
    ("Interface Down", "Notice"),
]


def pool_node(prefix: str, group: str) -> SolarWindsNode:
    """Узел с произвольным номером: `gpn-net-msk-23`, `gpn-web-lb-04`."""
    index = random.randint(1, 40)
    return SolarWindsNode(
        node=f"{prefix}-{index:02d}",
        ip=f"10.30.{index}.{random.randint(1, 250)}",
        group=group,
        alerts=SOLARWINDS_ALERTS,
    )


def random_solarwinds_node() -> SolarWindsNode:
    """Узел из пула площадок: даёт потоку неисчерпаемый запас новых пар."""
    site, group = random.choice(SOLARWINDS_SITES)
    return pool_node(f"gpn-net-{site}", group)

# --------------------------------------------------------------------------
# Сборка payload'ов
#
# Формы повторяют то, что реально настроено на стендах: media type Zabbix 7 из
# `deploy/integrations/zabbix7-webhook.js` и тела действий SolarWinds из
# `deploy/integrations/solarwinds-{alert,reset}-body.json`.
# --------------------------------------------------------------------------


def _now_local() -> datetime:
    """Локальное время сервера мониторинга: в payload таймзоны нет."""
    return datetime.now(tz=TZ).replace(tzinfo=None)


def zabbix_payload(
    *,
    event_id: str,
    host: ZabbixHost,
    rule: ZabbixRule,
    resolved: bool,
    value: str | None = None,
) -> dict[str, Any]:
    moment = _now_local()
    metric = value if value is not None else str(random.randint(*rule.value_range))
    return {
        "event_id": event_id,
        "host": host.host,
        "host_technical_name": host.host,
        "host_ip": "*UNKNOWN*",
        "hostgroup": host.hostgroup,
        "trigger_id": rule.trigger_id,
        "trigger_name": rule.name,
        "trigger_expression": f"last(/{host.host}/sim.metric)>{metric}",
        "severity": rule.severity_label,
        "event_nseverity": rule.nseverity,
        "status": "RESOLVED" if resolved else "PROBLEM",
        "event_value": "0" if resolved else "1",
        "timestamp": moment.strftime("%Y.%m.%d %H:%M:%S"),
        "date": moment.strftime("%Y.%m.%d"),
        "time": moment.strftime("%H:%M:%S"),
        "item_name": "Показатель мониторинга",
        "item_key": "sim.metric",
        "item_lastvalue": metric,
        "tags": host.tags,
        "message": (
            f"{'Resolved' if resolved else 'Problem'}: {rule.name}\n"
            f"Host: {host.host}\nValue: {metric}"
        ),
    }


def solarwinds_payload(
    *,
    node: SolarWindsNode,
    alert_name: str,
    severity: str,
    resolved: bool,
) -> dict[str, Any]:
    moment = _now_local()
    return {
        # eventType — то поле, по которому адаптер отличает закрытие от проблемы
        "eventType": "reset" if resolved else "trigger",
        "name": alert_name,
        "severity": severity,
        "message": (
            f"{alert_name} on {node.node} "
            f"{'cleared' if resolved else 'triggered'} ({random.randint(2, 97)}%)"
        ),
        "description": f"Alert {alert_name} for node {node.node}",
        # objectid боевой стенд не передаёт, роль идентификатора играет имя узла
        "node": node.node,
        "ip": node.ip,
        "group": node.group,
        "time": moment.strftime("%m/%d/%Y %I:%M:%S %p"),
    }


# --------------------------------------------------------------------------
# Отправка
# --------------------------------------------------------------------------


@dataclass
class Stats:
    sent: int = 0
    accepted: int = 0
    rejected: int = 0
    problems: int = 0
    resolutions: int = 0
    last_error: str | None = None
    last_sent_at: str | None = None
    by_status: dict[str, int] = field(default_factory=dict)


stats = Stats()


async def post_event(
    client: httpx.AsyncClient, source: Source, payload: dict[str, Any]
) -> int:
    """Один вебхук. Отправляем ровно так же, как это делают боевые системы."""
    body = json.dumps(payload, ensure_ascii=False).encode()
    headers = {"Content-Type": "application/json"}

    token = TOKENS.get(source, "")
    if token:
        headers["X-Webhook-Token"] = token
    secret = HMAC_SECRETS.get(source, "")
    if secret:
        # Секрет объявлен hex-строкой — так же его читает `app.cli sync-tokens`
        headers["X-Webhook-Signature"] = hmac.new(
            bytes.fromhex(secret), body, hashlib.sha256
        ).hexdigest()

    stats.sent += 1
    stats.last_sent_at = datetime.now(tz=TZ).isoformat(timespec="seconds")
    try:
        response = await client.post(
            f"{TARGET_URL}/api/v1/ingest/{source}", content=body, headers=headers
        )
    except httpx.HTTPError as exc:
        stats.rejected += 1
        stats.last_error = f"{type(exc).__name__}: {exc}"
        logger.warning("не удалось отправить событие %s: %s", source, exc)
        return 0

    code = str(response.status_code)
    stats.by_status[code] = stats.by_status.get(code, 0) + 1
    if response.status_code == 202:
        stats.accepted += 1
    else:
        stats.rejected += 1
        stats.last_error = f"{source}: HTTP {response.status_code}"
        # Тело ответа в лог не пишем: там может быть эхо payload'а
        logger.warning("приёмник отклонил событие %s: %s", source, response.status_code)
    return response.status_code


# --------------------------------------------------------------------------
# Состояние: какие проблемы эмулятор считает открытыми
# --------------------------------------------------------------------------


@dataclass
class OpenProblem:
    source: Source
    key: str
    payload_args: dict[str, Any]
    opened_at: datetime
    repeats: int = 0


class Fleet:
    """Открытые проблемы: из них берутся повторы и закрытия."""

    def __init__(self) -> None:
        self._open: dict[str, OpenProblem] = {}
        self._counter = 0

    @property
    def open_count(self) -> int:
        return len(self._open)

    def snapshot(self) -> list[dict[str, Any]]:
        return [
            {
                "source": item.source,
                "key": item.key,
                "repeats": item.repeats,
                "opened_at": item.opened_at.isoformat(timespec="seconds"),
            }
            for item in self._open.values()
        ]

    def next_event_id(self) -> str:
        self._counter += 1
        return f"sim-{int(datetime.now(tz=TZ).timestamp())}-{self._counter}"

    def register(self, problem: OpenProblem) -> None:
        self._open[problem.key] = problem

    def pick_open(self, source: Source | None = None) -> OpenProblem | None:
        candidates = [
            item
            for item in self._open.values()
            if source is None or item.source == source
        ]
        return random.choice(candidates) if candidates else None

    def close(self, key: str) -> OpenProblem | None:
        return self._open.pop(key, None)

    def all_open(self) -> list[OpenProblem]:
        return list(self._open.values())


fleet = Fleet()


async def open_zabbix(
    client: httpx.AsyncClient,
    host: ZabbixHost,
    rule: ZabbixRule,
) -> str:
    event_id = fleet.next_event_id()
    payload = zabbix_payload(event_id=event_id, host=host, rule=rule, resolved=False)
    await post_event(client, "zabbix", payload)
    stats.problems += 1
    fleet.register(
        OpenProblem(
            source="zabbix",
            key=event_id,
            payload_args={"host": host, "rule": rule, "event_id": event_id},
            opened_at=datetime.now(tz=TZ),
        )
    )
    return event_id


async def open_solarwinds(
    client: httpx.AsyncClient,
    node: SolarWindsNode,
    alert_name: str,
    severity: str,
) -> str:
    # Ключ дедупликации у SolarWinds — узел + имя алерта, повторное открытие
    # той же пары ляжет в тот же инцидент. Это и есть штатное поведение.
    key = f"solarwinds|{node.node}|{alert_name}"
    payload = solarwinds_payload(
        node=node, alert_name=alert_name, severity=severity, resolved=False
    )
    await post_event(client, "solarwinds", payload)
    stats.problems += 1
    fleet.register(
        OpenProblem(
            source="solarwinds",
            key=key,
            payload_args={"node": node, "alert_name": alert_name, "severity": severity},
            opened_at=datetime.now(tz=TZ),
        )
    )
    return key


async def repeat(client: httpx.AsyncClient, problem: OpenProblem) -> None:
    """Повтор той же проблемы: в окне подавления он инкрементит счётчик."""
    problem.repeats += 1
    if problem.source == "zabbix":
        payload = zabbix_payload(resolved=False, **problem.payload_args)
        await post_event(client, "zabbix", payload)
    else:
        payload = solarwinds_payload(resolved=False, **problem.payload_args)
        await post_event(client, "solarwinds", payload)


async def resolve(client: httpx.AsyncClient, problem: OpenProblem) -> None:
    if problem.source == "zabbix":
        payload = zabbix_payload(resolved=True, value="0", **problem.payload_args)
        await post_event(client, "zabbix", payload)
    else:
        payload = solarwinds_payload(resolved=True, **problem.payload_args)
        await post_event(client, "solarwinds", payload)
    stats.resolutions += 1
    fleet.close(problem.key)


async def emit_one(client: httpx.AsyncClient) -> str:
    """Один шаг потока. Возвращает описание того, что сделали."""
    problem = fleet.pick_open() if random.random() < DUP_RATIO else None

    if problem is not None:
        if random.random() < RESOLVE_RATIO:
            await resolve(client, problem)
            return f"resolved {problem.key}"
        await repeat(client, problem)
        return f"repeat {problem.key}"

    if random.random() < 0.6:
        host = random.choice(ZABBIX_FLEET)
        rule = random.choice(host.rules)
        key = await open_zabbix(client, host, rule)
        return f"problem zabbix {host.host} / {rule.name} ({key})"

    # Треть событий — по постоянному составу (он же в демо-данных), остальные
    # по пулу: иначе SolarWinds быстро исчерпает пары «узел + алерт»
    node = (
        random.choice(SOLARWINDS_FLEET)
        if random.random() < 0.35
        else random_solarwinds_node()
    )
    alert_name, severity = random.choice(node.alerts)
    await open_solarwinds(client, node, alert_name, severity)
    return f"problem solarwinds {node.node} / {alert_name}"


# --------------------------------------------------------------------------
# Сценарии для показа
#
# Каждый подобран под конкретную тему из `app.seed`: жмёшь — и объясняешь,
# почему уведомление пришло именно этому дежурному.
# --------------------------------------------------------------------------

SCENARIOS: dict[str, str] = {
    "scada-down": (
        "АСУ ТП Приобье: узел недоступен + перегрев соседнего "
        "→ темы scada-asu, critical-any"
    ),
    "network-msk": "Магистраль Москва: узел лёг, на соседнем потери пакетов → тема network-core",
    "db-replication": (
        "БД prod: остановлена репликация, теги sla+prod "
        "→ темы prod-db, sla-watch, critical-any"
    ),
    "web-degradation": (
        "Веб prod: рост отклика на сервере и балансировщике "
        "→ тема web-prod (два источника)"
    ),
    "noise": "Пять событий уровня Information со stage — шум, который темы обязаны отсечь",
    "storm": "Шторм: 50 событий подряд, из них большинство повторы — дедупликация в работе",
    "resolve-all": "Закрыть все проблемы, открытые эмулятором",
}


async def run_scenario(client: httpx.AsyncClient, name: str) -> list[str]:
    steps: list[str] = []

    def zbx(host_name: str) -> ZabbixHost:
        return next(item for item in ZABBIX_FLEET if item.host == host_name)

    if name == "scada-down":
        host = zbx("gpn-scada-07")
        steps.append(await open_zabbix(client, host, host.rules[0]))
        neighbour = zbx("gpn-scada-11")
        steps.append(await open_zabbix(client, neighbour, neighbour.rules[0]))

    elif name == "network-msk":
        # Узлы берутся из пула, а не фиксированные: пара «узел + алерт» — это
        # ключ дедупликации SolarWinds, и на постоянных именах сценарий
        # отработал бы ровно один раз, а со второго показа давал бы только
        # счётчик повторов без единого уведомления
        steps.append(
            await open_solarwinds(
                client, pool_node("gpn-net-msk", "Сеть / Магистраль"), "Node Down", "Critical"
            )
        )
        steps.append(
            await open_solarwinds(
                client, pool_node("gpn-net-msk", "Сеть / Магистраль"), "Packet Loss", "Serious"
            )
        )

    elif name == "db-replication":
        host = zbx("gpn-db-prod-05")
        steps.append(await open_zabbix(client, host, host.rules[0]))

    elif name == "web-degradation":
        host = zbx("gpn-web-01")
        steps.append(await open_zabbix(client, host, host.rules[0]))
        steps.append(
            await open_solarwinds(
                client,
                pool_node("gpn-web-lb", "Веб prod"),
                "Response Time High",
                "Warning",
            )
        )

    elif name == "noise":
        host = zbx("gpn-app-09")
        for _ in range(5):
            steps.append(await open_zabbix(client, host, random.choice(host.rules)))

    elif name == "storm":
        for _ in range(50):
            steps.append(await emit_one(client))
            await asyncio.sleep(0.05)

    elif name == "resolve-all":
        for problem in fleet.all_open():
            await resolve(client, problem)
            steps.append(f"resolved {problem.key}")

    else:
        raise KeyError(name)

    return steps


# --------------------------------------------------------------------------
# Сервис
# --------------------------------------------------------------------------


class Runner:
    def __init__(self) -> None:
        self.running = AUTOSTART
        self.client: httpx.AsyncClient | None = None
        self._task: asyncio.Task[None] | None = None

    async def loop(self) -> None:
        assert self.client is not None
        # Первый шаг с задержкой: приёмник может ещё подниматься
        await asyncio.sleep(min(INTERVAL_S, 10))
        while True:
            if self.running:
                try:
                    what = await emit_one(self.client)
                    logger.info("поток: %s", what)
                except Exception:  # поток не должен умирать от одного события
                    logger.exception("шаг потока завершился ошибкой")
            await asyncio.sleep(INTERVAL_S)

    def start(self) -> None:
        self._task = asyncio.create_task(self.loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass


runner = Runner()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    missing = [source for source, token in TOKENS.items() if not token]
    if missing:
        logger.warning(
            "не задан токен источников: %s — события получат 401. "
            "Задать INGEST_TOKEN_* в .env и применить: "
            "docker compose exec backend python -m app.cli sync-tokens",
            ", ".join(missing),
        )
    runner.client = httpx.AsyncClient(timeout=10.0)
    runner.start()
    logger.info(
        "эмулятор запущен: цель %s, интервал %.0f с, поток %s",
        TARGET_URL,
        INTERVAL_S,
        "включён" if AUTOSTART else "на паузе",
    )
    try:
        yield
    finally:
        await runner.stop()
        if runner.client is not None:
            await runner.client.aclose()


app = FastAPI(title="Источники мониторинга (эмулятор)", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"status": "ok", "running": runner.running}


@app.get("/state")
async def state() -> dict[str, Any]:
    return {
        "running": runner.running,
        "target": TARGET_URL,
        "interval_s": INTERVAL_S,
        "open_problems": fleet.open_count,
        "stats": {
            "sent": stats.sent,
            "accepted": stats.accepted,
            "rejected": stats.rejected,
            "problems": stats.problems,
            "resolutions": stats.resolutions,
            "by_status": stats.by_status,
            "last_sent_at": stats.last_sent_at,
            "last_error": stats.last_error,
        },
        "open": fleet.snapshot(),
    }


@app.get("/scenarios")
async def scenarios() -> dict[str, str]:
    return SCENARIOS


@app.post("/scenario/{name}")
async def scenario(name: str) -> Any:
    if runner.client is None:
        return JSONResponse(status_code=503, content={"error": "клиент не готов"})
    try:
        steps = await run_scenario(runner.client, name)
    except KeyError:
        return JSONResponse(
            status_code=404,
            content={"error": f"нет сценария '{name}'", "available": list(SCENARIOS)},
        )
    logger.info("сценарий %s: %d событий", name, len(steps))
    return {"scenario": name, "events": len(steps), "steps": steps}


@app.post("/pause")
async def pause() -> dict[str, Any]:
    runner.running = False
    logger.info("поток поставлен на паузу")
    return {"running": False}


@app.post("/resume")
async def resume() -> dict[str, Any]:
    runner.running = True
    logger.info("поток возобновлён")
    return {"running": True}
