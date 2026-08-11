"""Разбор результата нагрузочного прогона.

Считает по БД то, что нельзя увидеть со стороны генератора: сколько событий
дошло до инцидентов, во сколько раз сжался поток, уложились ли задержки
в целевые значения из раздела 2 docs/tz.md.

Запуск (из корня репозитория, окружение бэкенда):

    backend/.venv/Scripts/python tools/check_pipeline.py --minutes 15

Реквизиты БД берутся из --database-url, иначе из корневого .env
(порт по умолчанию — опубликованный оверлеем docker-compose.dev.yml).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

REPO_ROOT = Path(__file__).resolve().parent.parent

# Целевые показатели из раздела 2 docs/tz.md
TARGET_INGEST_TO_INCIDENT_S = 2.0
TARGET_INCIDENT_TO_INBOX_S = 5.0
TARGET_DEDUP_RATIO = 0.8


@dataclass
class Check:
    name: str
    value: str
    target: str = ""
    ok: bool | None = None


def dotenv() -> dict[str, str]:
    env_file = REPO_ROOT / ".env"
    if not env_file.exists():
        return {}
    values: dict[str, str] = {}
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()
    return values


def database_url(explicit: str | None) -> str:
    if explicit:
        return explicit
    env = dotenv()
    user = env.get("POSTGRES_USER", "apollo")
    password = env.get("POSTGRES_PASSWORD", "apollo")
    database = env.get("POSTGRES_DB", "apollo")
    port = os.environ.get("DB_PORT", "55432")
    return f"postgresql+asyncpg://{user}:{password}@127.0.0.1:{port}/{database}"


INGEST = text(
    """
    SELECT count(*) AS received,
           count(*) FILTER (WHERE processed_at IS NOT NULL) AS normalized,
           count(*) FILTER (WHERE process_error IS NOT NULL) AS failed,
           count(*) FILTER (WHERE processed_at IS NULL AND process_error IS NULL) AS pending,
           count(DISTINCT source) AS sources
    FROM raw_events
    WHERE received_at >= now() - make_interval(mins => :minutes)
    """
)

INCIDENTS = text(
    """
    SELECT count(*) AS incidents,
           coalesce(sum(dedup_count), 0) AS events_folded,
           count(*) FILTER (WHERE status = 'resolved') AS resolved,
           count(*) FILTER (WHERE is_test) AS test_incidents
    FROM incidents
    WHERE created_at >= now() - make_interval(mins => :minutes)
    """
)

FEED = text(
    """
    SELECT count(*) AS feed_records
    FROM incident_events
    WHERE received_at >= now() - make_interval(mins => :minutes)
    """
)

NOTIFICATIONS = text(
    """
    SELECT count(*) AS total,
           count(*) FILTER (WHERE status = 'sent') AS sent,
           count(*) FILTER (WHERE status = 'failed') AS failed,
           count(*) FILTER (WHERE status = 'pending') AS pending,
           count(*) FILTER (WHERE status = 'skipped') AS skipped
    FROM notifications
    WHERE created_at >= now() - make_interval(mins => :minutes)
    """
)

LATENCY = text(
    """
    SELECT
      percentile_cont(0.5)  WITHIN GROUP (ORDER BY value) AS p50,
      percentile_cont(0.95) WITHIN GROUP (ORDER BY value) AS p95,
      percentile_cont(0.99) WITHIN GROUP (ORDER BY value) AS p99,
      max(value) AS worst,
      count(*) AS samples
    FROM ({inner}) AS measured(value)
    """
)

INGEST_TO_NORMALIZED = """
    SELECT extract(epoch FROM (processed_at - received_at))
    FROM raw_events
    WHERE received_at >= now() - make_interval(mins => :minutes)
      AND processed_at IS NOT NULL
"""

INGEST_TO_INCIDENT = """
    SELECT extract(epoch FROM (i.created_at - r.received_at))
    FROM incidents i
    JOIN incident_events e ON e.incident_id = i.id
    JOIN raw_events r ON r.id = e.raw_event_id
    WHERE i.created_at >= now() - make_interval(mins => :minutes)
"""

INCIDENT_TO_INBOX = """
    SELECT extract(epoch FROM (n.sent_at - i.created_at))
    FROM notifications n
    JOIN incidents i ON i.id = n.incident_id
    WHERE n.created_at >= now() - make_interval(mins => :minutes)
      AND n.sent_at IS NOT NULL
"""


async def percentiles(engine, inner: str, minutes: int) -> dict[str, float | int]:
    async with engine.connect() as conn:
        row = (
            await conn.execute(text(LATENCY.text.format(inner=inner)), {"minutes": minutes})
        ).one()
    return {
        "samples": row.samples,
        "p50": row.p50 or 0.0,
        "p95": row.p95 or 0.0,
        "p99": row.p99 or 0.0,
        "worst": row.worst or 0.0,
    }


def verdict(ok: bool | None) -> str:
    if ok is None:
        return "   "
    return " ✓ " if ok else " ✗ "


def show(title: str, checks: list[Check]) -> bool:
    print()
    print(title)
    print("-" * 74)
    all_ok = True
    for check in checks:
        if check.ok is False:
            all_ok = False
        print(f"{verdict(check.ok)} {check.name:<44} {check.value:>12} {check.target}")
    return all_ok


async def main() -> int:
    parser = argparse.ArgumentParser(description="Разбор нагрузочного прогона")
    parser.add_argument("--minutes", type=int, default=15, help="окно анализа")
    parser.add_argument("--database-url", default=None)
    parser.add_argument("--expected-sent", type=int, default=0, help="сколько отправил генератор")
    # Потолок коэффициента задаёт сам прогон: при доле повторов d выше d
    # он подняться не может. Цель имеет смысл сверять с параметрами генератора
    parser.add_argument(
        "--target-dedup", type=float, default=TARGET_DEDUP_RATIO, help="ожидаемый коэффициент"
    )
    args = parser.parse_args(sys.argv[1:])

    engine = create_async_engine(database_url(args.database_url))
    minutes = args.minutes

    try:
        async with engine.connect() as conn:
            ingest = (await conn.execute(INGEST, {"minutes": minutes})).one()
            incidents = (await conn.execute(INCIDENTS, {"minutes": minutes})).one()
            feed = (await conn.execute(FEED, {"minutes": minutes})).one()
            notifications = (await conn.execute(NOTIFICATIONS, {"minutes": minutes})).one()

        normalization = await percentiles(engine, INGEST_TO_NORMALIZED, minutes)
        to_incident = await percentiles(engine, INGEST_TO_INCIDENT, minutes)
        to_inbox = await percentiles(engine, INCIDENT_TO_INBOX, minutes)
    finally:
        await engine.dispose()

    print("=" * 74)
    print(f"РАЗБОР КОНВЕЙЕРА ЗА ПОСЛЕДНИЕ {minutes} МИН")
    print("=" * 74)

    dedup_ratio = 1 - incidents.incidents / ingest.received if ingest.received else 0.0
    reduction = ingest.received / incidents.incidents if incidents.incidents else 1.0

    checks_ingest = [
        Check("принято событий", str(ingest.received)),
        Check(
            "нормализовано",
            str(ingest.normalized),
            "(должно совпасть с принятым)",
            ingest.normalized == ingest.received,
        ),
        Check("ошибок нормализации", str(ingest.failed), "", ingest.failed == 0),
        Check("осталось в очереди", str(ingest.pending), "", ingest.pending == 0),
        Check("источников задействовано", str(ingest.sources)),
    ]
    if args.expected_sent:
        checks_ingest.insert(
            0,
            Check(
                "отправлено генератором",
                str(args.expected_sent),
                "(потерь быть не должно)",
                ingest.received >= args.expected_sent,
            ),
        )
    ok_ingest = show("ПРИЁМ И НОРМАЛИЗАЦИЯ", checks_ingest)

    # Каждое обработанное событие даёт ровно одну запись в ленте: расхождение
    # означало бы либо потерю, либо повторную обработку
    ok_integrity = show(
        "ЦЕЛОСТНОСТЬ: НИ ПОТЕРЬ, НИ ДУБЛЕЙ",
        [
            Check("обработано событий", str(ingest.normalized)),
            Check(
                "записей в лентах инцидентов",
                str(feed.feed_records),
                "(равно обработанным)",
                feed.feed_records == ingest.normalized,
            ),
        ],
    )

    ok_dedup = show(
        "ДЕДУПЛИКАЦИЯ",
        [
            Check("создано инцидентов", str(incidents.incidents)),
            Check("из них закрыто", str(incidents.resolved)),
            Check("тестовых инцидентов", str(incidents.test_incidents)),
            Check(
                "коэффициент дедупликации",
                f"{dedup_ratio:.2f}",
                f"цель ≥ {args.target_dedup}",
                dedup_ratio >= args.target_dedup,
            ),
            Check("шума меньше в", f"{reduction:.1f}×"),
        ],
    )

    ok_delivery = show(
        "ДОСТАВКА",
        [
            Check("уведомлений создано", str(notifications.total)),
            Check("доставлено", str(notifications.sent)),
            Check("ошибок доставки", str(notifications.failed), "", notifications.failed == 0),
            Check("в очереди", str(notifications.pending)),
            Check("пропущено (глушение)", str(notifications.skipped)),
        ],
    )

    def latency_checks(name: str, data: dict[str, float | int], target: float | None) -> list[Check]:
        rows = [Check(f"{name}: замеров", str(data["samples"]))]
        for label, key in (("p50", "p50"), ("p95", "p95"), ("p99", "p99"), ("макс", "worst")):
            value = float(data[key])
            ok = None
            if target is not None and label in ("p95", "p99"):
                ok = value <= target
            rows.append(
                Check(
                    f"{name}: {label}",
                    f"{value:.2f} с",
                    f"цель ≤ {target} с" if ok is not None else "",
                    ok,
                )
            )
        return rows

    ok_latency = show(
        "ЗАДЕРЖКИ КОНВЕЙЕРА",
        [
            *latency_checks("приём → нормализация", normalization, None),
            *latency_checks("вебхук → инцидент", to_incident, TARGET_INGEST_TO_INCIDENT_S),
            *latency_checks("инцидент → инбокс", to_inbox, TARGET_INCIDENT_TO_INBOX_S),
        ],
    )

    print()
    print("=" * 74)
    passed = all([ok_ingest, ok_integrity, ok_dedup, ok_delivery, ok_latency])
    print("ИТОГ: все проверки пройдены" if passed else "ИТОГ: есть непройденные проверки (см. ✗)")
    print("=" * 74)
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
