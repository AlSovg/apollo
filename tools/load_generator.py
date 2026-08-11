"""Генератор нагрузки: синтетические события на эндпоинты приёма.

Payload'ы структурно повторяют реальные вебхуки стенда, поэтому проверяется
не «умеет ли система принимать JSON», а весь конвейер целиком.

Запуск (из корня репозитория, окружение бэкенда):

    backend/.venv/Scripts/python tools/load_generator.py \\
        --rate 50 --duration 20 --dup-ratio 0.6 --resolve-ratio 0.2 \\
        --issue-tokens --admin-user admin --admin-password ...

Флаг is_test переключаемый:
  --test (по умолчанию) события помечены тестовыми: они не попадают в дашборд
         инцидентов и НЕ порождают уведомлений (FR-INC-7), поэтому измеряются
         только приём и нормализация;
  --real события идут как боевые — так замеряется сквозной путь до инбокса
         вместе с маршрутизацией и доставкой.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import statistics
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

import httpx

# Группа тестовых хостов Zabbix: по ней адаптер выставляет is_test
TEST_HOST_GROUP = "Test webhook group"
REAL_HOST_GROUP = "prod-db"

RULES = [
    ("Диск заполнен на 94%", "4", "High"),
    ("Высокая загрузка CPU", "3", "Average"),
    ("Недоступен узел", "5", "Disaster"),
    ("Растёт время отклика", "2", "Warning"),
]


@dataclass
class Sample:
    status: int
    latency_ms: float


@dataclass
class Report:
    sent: int = 0
    accepted: int = 0
    statuses: Counter[int] = field(default_factory=Counter)
    latencies: list[float] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    started_at: float = 0.0
    finished_at: float = 0.0

    def add(self, sample: Sample) -> None:
        self.sent += 1
        self.statuses[sample.status] += 1
        self.latencies.append(sample.latency_ms)
        if sample.status == 202:
            self.accepted += 1

    @property
    def duration_s(self) -> float:
        return max(self.finished_at - self.started_at, 1e-9)

    def percentile(self, share: float) -> float:
        if not self.latencies:
            return 0.0
        ordered = sorted(self.latencies)
        index = min(int(len(ordered) * share), len(ordered) - 1)
        return ordered[index]


class EventFactory:
    """Синтетические события трёх источников с повторами и восстановлениями."""

    def __init__(self, *, is_test: bool, dup_ratio: float, resolve_ratio: float) -> None:
        self.is_test = is_test
        self.dup_ratio = dup_ratio
        self.resolve_ratio = resolve_ratio
        self._counter = 0
        # Ключи уже отправленных проблем: из них берутся повторы и закрытия
        self._open_keys: dict[str, list[str]] = {"zabbix": [], "solarwinds": [], "prometheus": []}

    def _next_key(self, source: str) -> tuple[str, bool]:
        """Ключ события и признак «это повтор уже открытой проблемы»."""
        known = self._open_keys[source]
        if known and random.random() < self.dup_ratio:
            return random.choice(known), True

        self._counter += 1
        key = f"gen-{self._counter}"
        known.append(key)
        if len(known) > 200:
            known.pop(0)
        return key, False

    def build(self, source: str) -> tuple[str, dict[str, object] | None, dict[str, str] | None]:
        """Возвращает (путь, тело JSON, query-параметры)."""
        key, repeated = self._next_key(source)
        resolving = repeated and random.random() < self.resolve_ratio
        rule, nseverity, severity = random.choice(RULES)
        moment = datetime.now(tz=UTC)

        if source == "zabbix":
            return "/api/v1/ingest/zabbix", self._zabbix(key, rule, nseverity, severity, resolving, moment), None
        if source == "solarwinds":
            return "/api/v1/ingest/solarwinds", None, self._solarwinds(key, rule, severity, resolving, moment)
        return "/api/v1/ingest/prometheus", self._prometheus(key, rule, resolving, moment), None

    def _zabbix(
        self,
        key: str,
        rule: str,
        nseverity: str,
        severity: str,
        resolving: bool,
        moment: datetime,
    ) -> dict[str, object]:
        host = f"gen-host-{hash(key) % 40:02d}"
        return {
            "event_id": key,
            "host": host,
            "host_technical_name": host,
            "host_ip": f"10.20.{hash(key) % 250}.{hash(rule) % 250}",
            "host_dns": "*UNKNOWN*",
            "hostgroup": TEST_HOST_GROUP if self.is_test else REAL_HOST_GROUP,
            "trigger_id": str(20000 + hash(rule) % 999),
            "trigger_name": rule,
            "trigger_expression": f"last(/{host}/gen.metric)>90",
            "trigger_url": "",
            "event_name": rule,
            "event_nseverity": nseverity,
            "severity": severity,
            "status": "RESOLVED" if resolving else "PROBLEM",
            "event_value": "0" if resolving else "1",
            # Локальное время сервера Zabbix, без таймзоны — как в реальном вебхуке
            "timestamp": moment.strftime("%Y.%m.%d %H:%M:%S"),
            "date": moment.strftime("%Y.%m.%d"),
            "time": moment.strftime("%H:%M:%S"),
            "duration": "0s",
            "opdata": "",
            "tags": ["generated", "load"],
            "item_name": "Сгенерированная метрика",
            "item_key": "gen.metric",
            "item_lastvalue": str(random.randint(80, 99)),
            "message": f"{'Resolved' if resolving else 'Problem'}: {rule}",
        }

    def _solarwinds(
        self, key: str, rule: str, severity: str, resolving: bool, moment: datetime
    ) -> dict[str, str]:
        return {
            "name": rule,
            "severity": "Critical" if severity in ("High", "Disaster") else "Warning",
            "status": "Reset" if resolving else "Triggered",
            "node": f"gen-node-{hash(key) % 30:02d}",
            "nodeid": str(1000 + hash(key) % 30),
            "ip": f"10.30.{hash(key) % 250}.10",
            "cpu": str(random.randint(70, 99)),
            "time": moment.strftime("%m/%d/%Y %I:%M:%S %p"),
        }

    def _prometheus(
        self, key: str, rule: str, resolving: bool, moment: datetime
    ) -> dict[str, object]:
        return {
            "status": "resolved" if resolving else "firing",
            "alerts": [
                {
                    "status": "resolved" if resolving else "firing",
                    "labels": {
                        "alertname": rule,
                        "instance": f"gen-app-{hash(key) % 25:02d}:9100",
                        "severity": "critical" if "Недоступен" in rule else "warning",
                    },
                    "annotations": {"summary": f"{rule} (нагрузочный прогон)"},
                    "startsAt": moment.isoformat(),
                    "fingerprint": key,
                }
            ],
            "commonLabels": {"severity": "warning"},
        }


async def issue_tokens(
    client: httpx.AsyncClient, sources: list[str], username: str, password: str
) -> dict[str, str]:
    """Логинится администратором и ротирует токены нужных источников."""
    response = await client.post(
        "/api/v1/auth/login", json={"username": username, "password": password}
    )
    if response.status_code != 200:
        raise SystemExit(f"Не удалось войти администратором: {response.status_code}")

    tokens: dict[str, str] = {}
    for source in sources:
        rotated = await client.post(f"/api/v1/admin/sources/{source}/rotate-token")
        if rotated.status_code != 200:
            raise SystemExit(
                f"Не удалось выдать токен источнику '{source}': {rotated.status_code}"
            )
        tokens[source] = rotated.json()["token"]
    return tokens


async def send_one(
    client: httpx.AsyncClient,
    factory: EventFactory,
    source: str,
    tokens: dict[str, str],
    report: Report,
    gate: asyncio.Semaphore,
) -> None:
    path, body, params = factory.build(source)
    url = path if params is None else f"{path}?{urlencode(params)}"

    # Семафор держит число запросов «в полёте» и, главное, отсекает от замера
    # ожидание свободного клиентского соединения: иначе в задержку приёма
    # попадала бы очередь внутри самого генератора
    async with gate:
        started = time.perf_counter()
        try:
            response = await client.post(
                url,
                content=json.dumps(body, ensure_ascii=False).encode() if body else b"",
                headers={
                    "X-Webhook-Token": tokens[source],
                    **({"Content-Type": "application/json"} if body else {}),
                },
            )
            status = response.status_code
        except (httpx.TransportError, httpx.TimeoutException) as exc:
            status = 0
            report.errors.append(type(exc).__name__)

        report.add(Sample(status=status, latency_ms=(time.perf_counter() - started) * 1000))


async def run(args: argparse.Namespace) -> Report:
    sources = [item.strip() for item in args.sources.split(",") if item.strip()]
    factory = EventFactory(
        is_test=args.test, dup_ratio=args.dup_ratio, resolve_ratio=args.resolve_ratio
    )
    report = Report()

    limits = httpx.Limits(max_connections=args.concurrency, max_keepalive_connections=args.concurrency)
    async with httpx.AsyncClient(base_url=args.base_url, timeout=15.0, limits=limits) as client:
        tokens = dict(token.split("=", 1) for token in args.token)
        if args.issue_tokens:
            tokens |= await issue_tokens(client, sources, args.admin_user, args.admin_password)

        missing = [source for source in sources if source not in tokens]
        if missing:
            raise SystemExit(
                f"Нет токенов для источников: {', '.join(missing)}. "
                "Передайте --token источник=значение или --issue-tokens."
            )

        total = args.total or int(args.rate * args.duration)
        # Поток раскладывается по тикам, чтобы держать заданную частоту, а не
        # выстреливать всё разом: иначе замеряется не система, а очередь клиента
        tick = 0.1
        per_tick = max(1, round(args.rate * tick))
        gate = asyncio.Semaphore(args.concurrency)

        report.started_at = time.perf_counter()
        deadline = report.started_at + args.duration if not args.total else None

        created = 0
        pending: set[asyncio.Task[None]] = set()
        while created < total:
            if deadline and time.perf_counter() > deadline:
                break
            batch_started = time.perf_counter()
            for _ in range(min(per_tick, total - created)):
                task = asyncio.create_task(
                    send_one(client, factory, random.choice(sources), tokens, report, gate)
                )
                pending.add(task)
                task.add_done_callback(pending.discard)
                created += 1

            elapsed = time.perf_counter() - batch_started
            if elapsed < tick:
                await asyncio.sleep(tick - elapsed)

        if pending:
            await asyncio.gather(*pending)
        report.finished_at = time.perf_counter()

    return report


def print_report(report: Report, args: argparse.Namespace) -> None:
    print()
    print("=" * 62)
    print("ОТЧЁТ ГЕНЕРАТОРА")
    print("=" * 62)
    print(f"  режим событий:      {'тестовые (без уведомлений)' if args.test else 'боевые'}")
    print(f"  отправлено:         {report.sent}")
    print(f"  принято (202):      {report.accepted}")
    print(f"  длительность:       {report.duration_s:.1f} с")
    print(f"  фактическая частота: {report.sent / report.duration_s:.1f} событий/с")
    print()
    print("  коды ответов:")
    for status, count in sorted(report.statuses.items()):
        label = "сетевая ошибка" if status == 0 else str(status)
        print(f"    {label:>16}: {count}")
    if report.errors:
        print(f"  сетевых ошибок: {len(report.errors)} ({Counter(report.errors).most_common(3)})")
    print()
    print("  задержка ответа приёма, мс:")
    print(f"    p50 {report.percentile(0.5):8.1f}")
    print(f"    p95 {report.percentile(0.95):8.1f}")
    print(f"    p99 {report.percentile(0.99):8.1f}")
    print(f"    max {max(report.latencies, default=0):8.1f}")
    print(f"    средняя {statistics.fmean(report.latencies) if report.latencies else 0:.1f}")
    print("=" * 62)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Генератор нагрузки для приёма событий")
    # Адрес по IPv4 намеренно: на Windows localhost резолвится в ::1, где
    # проброс портов Docker Desktop иногда не отвечает
    parser.add_argument("--base-url", default="http://127.0.0.1")
    parser.add_argument("--rate", type=float, default=50, help="событий в секунду")
    parser.add_argument("--duration", type=float, default=20, help="секунд")
    parser.add_argument("--total", type=int, default=0, help="всего событий (важнее duration)")
    parser.add_argument("--sources", default="zabbix,solarwinds,prometheus")
    parser.add_argument("--dup-ratio", type=float, default=0.6, help="доля повторов")
    parser.add_argument("--resolve-ratio", type=float, default=0.2, help="доля восстановлений")
    parser.add_argument("--concurrency", type=int, default=50)
    parser.add_argument(
        "--token", action="append", default=[], metavar="ИСТОЧНИК=ТОКЕН"
    )
    parser.add_argument("--issue-tokens", action="store_true", help="выдать токены через админку")
    parser.add_argument("--admin-user", default="admin")
    parser.add_argument("--admin-password", default="")

    test_group = parser.add_mutually_exclusive_group()
    test_group.add_argument(
        "--test",
        dest="test",
        action="store_true",
        help="пометить события тестовыми: без уведомлений и вне дашборда",
    )
    test_group.add_argument(
        "--real",
        dest="test",
        action="store_false",
        help="боевые события: замеряется сквозной путь до инбокса",
    )
    parser.set_defaults(test=True)

    return parser.parse_args(argv)


def main() -> None:
    args = parse_args(sys.argv[1:])
    print(
        f"Прогон: источники {args.sources}, {args.rate} событий/с, "
        f"повторов {args.dup_ratio:.0%}, восстановлений {args.resolve_ratio:.0%}"
    )
    started = datetime.now(tz=UTC)
    report = asyncio.run(run(args))
    print_report(report, args)
    print(
        f"\nОкно прогона: с {started:%H:%M:%S} по {datetime.now(tz=UTC):%H:%M:%S} UTC "
        f"(для check_pipeline.py --minutes {max(1, int((datetime.now(tz=UTC) - started + timedelta(minutes=1)).total_seconds() // 60))})"
    )


if __name__ == "__main__":
    main()
