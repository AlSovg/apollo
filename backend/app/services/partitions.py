"""Управление месячными партициями raw_events и incident_events.

Партиции создаются заранее (вставка в несуществующую партицию — ошибка приёма
события) и дропаются по ретеншену: `raw_events` живёт 30 дней, дроп партиции
дешевле, чем DELETE по миллионам строк.

Имена партиций детерминированы: `{таблица}_{ГГГГ}_{ММ}`. DDL нельзя
параметризовать, поэтому имена таблиц проверяются по списку разрешённых,
а имена партиций строятся только этим модулем — пользовательский ввод сюда
не попадает.
"""

import logging
import re
from datetime import date

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.db import get_engine

logger = logging.getLogger(__name__)

# Партиционированные таблицы: только эти имена допустимы в DDL
PARTITIONED_TABLES: tuple[str, ...] = ("raw_events", "incident_events")

_PARTITION_NAME_RE = re.compile(r"^(?P<table>[a-z_]+)_(?P<year>\d{4})_(?P<month>\d{2})$")

# Партиция или её индекс: raw_events_2026_08, raw_events_2026_08_payload_idx
_PARTITION_OBJECT_RE = re.compile(
    r"^(?:raw_events|incident_events)_\d{4}_\d{2}(?:_.+)?$"
)


def is_partition_object(name: str) -> bool:
    """Партиции и их индексы создаются кодом, а не ORM: autogenerate их игнорирует."""
    return _PARTITION_OBJECT_RE.match(name) is not None


def month_start(day: date) -> date:
    return day.replace(day=1)


def add_months(month: date, count: int) -> date:
    """Сдвиг на count месяцев от первого числа месяца."""
    total = (month.year * 12 + month.month - 1) + count
    return date(total // 12, total % 12 + 1, 1)


def partition_name(table: str, month: date) -> str:
    return f"{table}_{month.year:04d}_{month.month:02d}"


def parse_partition_month(name: str) -> date | None:
    """Месяц партиции по её имени. None, если имя не наше."""
    match = _PARTITION_NAME_RE.match(name)
    if match is None:
        return None
    year, month = int(match["year"]), int(match["month"])
    if not 1 <= month <= 12:
        return None
    return date(year, month, 1)


def partition_plan(
    table: str, today: date, months_ahead: int
) -> list[tuple[str, date, date]]:
    """Партиции текущего месяца и следующих months_ahead: (имя, от, до)."""
    if table not in PARTITIONED_TABLES:
        raise ValueError(f"Неизвестная партиционированная таблица: {table}")
    if months_ahead < 0:
        raise ValueError("months_ahead не может быть отрицательным")

    current = month_start(today)
    plan: list[tuple[str, date, date]] = []
    for offset in range(months_ahead + 1):
        start = add_months(current, offset)
        plan.append((partition_name(table, start), start, add_months(start, 1)))
    return plan


def create_partition_ddl(table: str, name: str, start: date, end: date) -> str:
    if table not in PARTITIONED_TABLES:
        raise ValueError(f"Неизвестная партиционированная таблица: {table}")
    return (
        f"CREATE TABLE IF NOT EXISTS {name} PARTITION OF {table} "
        f"FOR VALUES FROM ('{start.isoformat()}') TO ('{end.isoformat()}')"
    )


def partition_ddl_batch(today: date, months_ahead: int) -> list[str]:
    """DDL всех недостающих партиций по всем партиционированным таблицам."""
    statements: list[str] = []
    for table in PARTITIONED_TABLES:
        for name, start, end in partition_plan(table, today, months_ahead):
            statements.append(create_partition_ddl(table, name, start, end))
    return statements


# Партиции, существование которых уже подтверждено в этом процессе.
# Без кеша нормализатор ходил бы в каталог БД на каждое событие: на потоке
# в сотни событий это сотни лишних запросов в секунду.
_KNOWN_PARTITIONS: set[str] = set()


async def ensure_partition_for(conn: AsyncConnection, table: str, moment: date) -> str | None:
    """Досоздать партицию под конкретную дату события.

    Время события берётся из источника, а не из наших часов: сбитые часы или
    доливка истории легко попадают в месяц, для которого партиции ещё нет,
    и без этой проверки вставка упала бы.
    """
    if table not in PARTITIONED_TABLES:
        raise ValueError(f"Неизвестная партиционированная таблица: {table}")

    start = month_start(moment)
    name = partition_name(table, start)
    if name in _KNOWN_PARTITIONS:
        return None

    exists = await conn.scalar(text("SELECT to_regclass(:name)"), {"name": name})
    if exists is not None:
        _KNOWN_PARTITIONS.add(name)
        return None

    await conn.execute(text(create_partition_ddl(table, name, start, add_months(start, 1))))
    _KNOWN_PARTITIONS.add(name)
    logger.info("partition created on demand", extra={"partition": name})
    return name


async def _list_partitions(conn: AsyncConnection, table: str) -> list[str]:
    result = await conn.execute(
        text(
            "SELECT c.relname "
            "FROM pg_inherits i "
            "JOIN pg_class c ON c.oid = i.inhrelid "
            "JOIN pg_class p ON p.oid = i.inhparent "
            "WHERE p.relname = :table"
        ),
        {"table": table},
    )
    return [row[0] for row in result]


async def ensure_partitions(months_ahead: int = 1, *, today: date | None = None) -> list[str]:
    """Создать недостающие партиции на текущий месяц и months_ahead вперёд.

    Вызывается при старте приложения: без партиции приём события упадёт.
    """
    statements = partition_ddl_batch(today or date.today(), months_ahead)
    async with get_engine().begin() as conn:
        for statement in statements:
            await conn.execute(text(statement))
    logger.info("partitions ensured", extra={"months_ahead": months_ahead})
    return statements


async def drop_old_partitions(
    retention_days: int, *, table: str = "raw_events", today: date | None = None
) -> list[str]:
    """Дропнуть партиции, целиком вышедшие за пределы ретеншена.

    Партиция дропается, только если её верхняя граница не позже даты отсечки:
    частично актуальные партиции не трогаются.
    """
    if table not in PARTITIONED_TABLES:
        raise ValueError(f"Неизвестная партиционированная таблица: {table}")
    if retention_days < 1:
        raise ValueError("retention_days должен быть положительным")

    cutoff = (today or date.today()).toordinal() - retention_days
    cutoff_date = date.fromordinal(cutoff)

    dropped: list[str] = []
    async with get_engine().begin() as conn:
        for name in await _list_partitions(conn, table):
            month = parse_partition_month(name)
            if month is None or add_months(month, 1) > cutoff_date:
                continue
            await conn.execute(text(f"DROP TABLE IF EXISTS {name}"))
            dropped.append(name)

    if dropped:
        # Факт дропа логируем: это безвозвратное удаление данных
        logger.info(
            "partitions dropped",
            extra={"table": table, "partitions": ",".join(dropped)},
        )
    return dropped
