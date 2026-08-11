from datetime import date

import pytest

from app.services.partitions import (
    add_months,
    create_partition_ddl,
    parse_partition_month,
    partition_ddl_batch,
    partition_name,
    partition_plan,
)


def test_add_months_crosses_year_boundary() -> None:
    assert add_months(date(2026, 12, 1), 1) == date(2027, 1, 1)
    assert add_months(date(2026, 1, 1), 12) == date(2027, 1, 1)
    assert add_months(date(2026, 8, 1), 0) == date(2026, 8, 1)


def test_partition_plan_covers_current_and_next_months() -> None:
    plan = partition_plan("raw_events", date(2026, 8, 10), months_ahead=1)
    assert plan == [
        ("raw_events_2026_08", date(2026, 8, 1), date(2026, 9, 1)),
        ("raw_events_2026_09", date(2026, 9, 1), date(2026, 10, 1)),
    ]


def test_partition_plan_rejects_unknown_table() -> None:
    with pytest.raises(ValueError):
        partition_plan("users", date(2026, 8, 10), months_ahead=1)


def test_create_partition_ddl_matches_schema_sql() -> None:
    name = partition_name("raw_events", date(2026, 8, 1))
    ddl = create_partition_ddl("raw_events", name, date(2026, 8, 1), date(2026, 9, 1))
    assert ddl == (
        "CREATE TABLE IF NOT EXISTS raw_events_2026_08 PARTITION OF raw_events "
        "FOR VALUES FROM ('2026-08-01') TO ('2026-09-01')"
    )


def test_partition_ddl_batch_covers_both_partitioned_tables() -> None:
    statements = partition_ddl_batch(date(2026, 8, 10), months_ahead=0)
    assert len(statements) == 2
    assert any("raw_events_2026_08" in s for s in statements)
    assert any("incident_events_2026_08" in s for s in statements)


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("raw_events_2026_08", date(2026, 8, 1)),
        ("incident_events_2026_12", date(2026, 12, 1)),
        ("raw_events_2026_13", None),  # несуществующий месяц
        ("raw_events_default", None),  # не наша партиция
        ("some_table", None),
    ],
)
def test_parse_partition_month(name: str, expected: date | None) -> None:
    assert parse_partition_month(name) == expected
