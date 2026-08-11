"""Демонстрационные данные стенда.

    python -m app.seed            # добавить недостающее
    python -m app.seed --reset    # сначала убрать прежние демо-данные

Справочники (роли, источники, карта критичности) приходят миграцией — здесь
только то, что миграцией быть не должно: темы, подписки и соответствие групп
тестового домена ролям. У заказчика DN другие, поэтому в бою используется
`python -m app.cli map-role`, а не этот файл.

Команда идемпотентна: повторный запуск ничего не дублирует.
"""

import argparse
import asyncio
import sys
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_engine, get_sessionmaker
from app.models.identity import RoleMapping
from app.models.subscriptions import Topic, TopicRole

# Тестовый домен apollo.test (deploy/ad). Структура групп повторяет боевую:
# админы платформы, дежурная смена, владельцы сервисов.
DEMO_ROLE_MAPPINGS: list[tuple[str, str]] = [
    ("CN=platform-admins,OU=apollo-groups,DC=apollo,DC=test", "admin"),
    ("CN=noc-engineers,OU=apollo-groups,DC=apollo,DC=test", "engineer"),
    ("CN=service-owners,OU=apollo-groups,DC=apollo,DC=test", "viewer"),
    # OpenLDAP-мок из dev-оверлея: другой формат DN, те же роли
    ("cn=platform-admins,ou=groups,dc=apollo,dc=test", "admin"),
    ("cn=noc-engineers,ou=groups,dc=apollo,dc=test", "engineer"),
    ("cn=service-owners,ou=groups,dc=apollo,dc=test", "viewer"),
]

# Две темы показывают обе стороны маршрутизации: узкий отбор с обязательной
# подпиской по роли и «ловит всё» для добровольной подписки.
DEMO_TOPICS: list[dict[str, Any]] = [
    {
        "code": "prod-db",
        "name": "БД prod",
        "description": "Инциденты Zabbix уровня «средняя» и выше",
        "match_rule": {"sources": ["zabbix"], "severity_min": 3},
        "mandatory_roles": ["engineer"],
    },
    {
        "code": "all-events",
        "name": "Все события",
        "description": "Пустое правило ловит всё — для добровольной подписки",
        "match_rule": {},
        "mandatory_roles": [],
    },
]


async def _reset(session: AsyncSession) -> None:
    """Убрать ровно демо-данные, не трогая то, что завели руками."""
    codes = [topic["code"] for topic in DEMO_TOPICS]
    topics = (
        await session.execute(select(Topic).where(Topic.code.in_(codes)))
    ).scalars().all()
    for topic in topics:
        # Подписки и обязательные роли уезжают каскадом по внешним ключам
        await session.delete(topic)

    await session.execute(
        delete(RoleMapping).where(
            RoleMapping.external_group.in_([group for group, _ in DEMO_ROLE_MAPPINGS])
        )
    )
    await session.flush()
    print(f"Удалено демо-тем: {len(topics)}")


async def _seed(session: AsyncSession) -> None:
    for mapping, role in DEMO_ROLE_MAPPINGS:
        await session.execute(
            insert(RoleMapping)
            .values(external_group=mapping, role_code=role)
            .on_conflict_do_nothing()
        )

    created = 0
    for spec in DEMO_TOPICS:
        topic = (
            await session.execute(select(Topic).where(Topic.code == spec["code"]))
        ).scalar_one_or_none()
        if topic is None:
            topic = Topic(
                code=spec["code"],
                name=spec["name"],
                description=spec["description"],
                match_rule=spec["match_rule"],
                is_active=True,
            )
            session.add(topic)
            await session.flush()
            created += 1

        for role_code in spec["mandatory_roles"]:
            await session.execute(
                insert(TopicRole)
                .values(topic_id=topic.id, role_code=role_code)
                .on_conflict_do_nothing()
            )

    await session.flush()
    print(f"Сопоставлений групп: {len(DEMO_ROLE_MAPPINGS)}, новых тем: {created}")


async def _main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="app.seed")
    parser.add_argument(
        "--reset", action="store_true", help="Сначала удалить прежние демо-данные"
    )
    args = parser.parse_args(argv)

    try:
        async with get_sessionmaker()() as session:
            if args.reset:
                await _reset(session)
            await _seed(session)
            await session.commit()
    finally:
        await get_engine().dispose()

    print("Готово. Подписки по ролям появятся у пользователей автоматически,")
    print("добровольные — на экране «Подписки».")
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(_main(sys.argv[1:])))


if __name__ == "__main__":
    main()
