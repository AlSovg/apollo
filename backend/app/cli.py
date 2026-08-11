"""Служебные команды бэкенда.

    python -m app.cli issue-token zabbix [--with-hmac]
    python -m app.cli create-admin breakglass [--password ...]
    python -m app.cli map-role "CN=platform-admins,OU=...,DC=..." admin
    python -m app.cli list-role-mappings

Секреты печатаются один раз и в БД хранятся только в виде хеша.
В репозиторий и в логи они не попадают. Постоянное место управления
токенами — админские эндпоинты.
"""

import argparse
import asyncio
import secrets
import string
import sys

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert

from app.adapters.identity import hash_password
from app.db import get_engine, get_sessionmaker
from app.models.events import EventSource
from app.models.identity import Role, RoleMapping, User, UserRole
from app.services.ingest import token_hash

_PASSWORD_ALPHABET = string.ascii_letters + string.digits


async def _issue_token(code: str, with_hmac: bool) -> int:
    token = secrets.token_urlsafe(32)
    hmac_secret = secrets.token_bytes(32) if with_hmac else None

    async with get_sessionmaker()() as session:
        source = await session.get(EventSource, code)
        if source is None:
            print(f"Источник '{code}' не зарегистрирован", file=sys.stderr)
            return 1

        # Записываются оба поля разом: иначе от прошлой выдачи остался бы
        # секрет, и напечатанный токен сам по себе не работал бы
        await session.execute(
            update(EventSource)
            .where(EventSource.code == code)
            .values(token_hash=token_hash(token), hmac_secret=hmac_secret)
        )
        await session.commit()

    print(f"source:      {code}")
    print(f"X-Webhook-Token: {token}")
    if hmac_secret is not None:
        print(f"HMAC secret (hex): {hmac_secret.hex()}")
        print("Подпись: X-Webhook-Signature = hex(hmac_sha256(secret, тело запроса))")
    else:
        print("HMAC-подпись: не требуется (секрет сброшен, см. --with-hmac)")
    print("Значения показаны один раз — сохраните их в настройках источника.")
    return 0


async def _create_admin(username: str, password: str | None) -> int:
    """Локальный администратор — предохранитель на случай недоступности каталога.

    В штатном режиме учётки приходят из каталога, но при AUTH_PROVIDER=ldap
    вход локальных учёток разрешён, только когда каталог не отвечает
    (services/login.py). Если такой учётки нет вовсе, падение каталога
    запирает систему целиком.
    """
    secret = password or "".join(secrets.choice(_PASSWORD_ALPHABET) for _ in range(20))

    async with get_sessionmaker()() as session:
        existing = (
            await session.execute(select(User).where(User.username == username))
        ).scalar_one_or_none()
        if existing is not None:
            if existing.provider != "local":
                print(
                    f"'{username}' уже занят доменной учёткой — выберите другой логин",
                    file=sys.stderr,
                )
                return 1
            # Смена пароля существующему: команда нужна и для восстановления
            # доступа, а не только для первичной установки
            await session.execute(
                update(User)
                .where(User.id == existing.id)
                .values(password_hash=hash_password(secret), is_active=True)
            )
            user_id = existing.id
            action = "обновлён"
        else:
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
            action = "создан"

        await session.execute(
            insert(UserRole)
            .values(user_id=user_id, role_code="admin", source="local")
            .on_conflict_do_nothing()
        )
        await session.commit()

    print(f"Локальный администратор {action}: {username}")
    if password is None:
        print(f"Пароль: {secret}")
        print("Пароль показан один раз — сохраните его в менеджере паролей.")
    return 0


async def _map_role(external_group: str, role_code: str) -> int:
    """Группа каталога → роль платформы.

    Без этой связи первый доменный вход создаёт пользователя без ролей:
    в админку не попасть, и починить нельзя ничем, кроме доступа к базе.
    """
    async with get_sessionmaker()() as session:
        role = await session.get(Role, role_code)
        if role is None:
            known = (await session.execute(select(Role.code))).scalars().all()
            print(
                f"Роль '{role_code}' неизвестна. Доступные: {', '.join(sorted(known))}",
                file=sys.stderr,
            )
            return 1

        await session.execute(
            insert(RoleMapping)
            .values(external_group=external_group, role_code=role_code)
            .on_conflict_do_nothing()
        )
        await session.commit()

    print(f"Группа сопоставлена роли: {external_group} → {role_code}")
    print("Роли применятся при следующем входе пользователя или синхронизации.")
    return 0


async def _unmap_role(external_group: str) -> int:
    async with get_sessionmaker()() as session:
        rows = (
            await session.execute(
                select(RoleMapping).where(RoleMapping.external_group == external_group)
            )
        ).scalars().all()
        for row in rows:
            await session.delete(row)
        await session.commit()
    print(f"Удалено сопоставлений: {len(rows)}")
    return 0


async def _list_role_mappings() -> int:
    async with get_sessionmaker()() as session:
        rows = (
            await session.execute(select(RoleMapping).order_by(RoleMapping.role_code))
        ).scalars().all()

    if not rows:
        print("Сопоставлений нет: доменные пользователи войдут без ролей.")
        print("Задайте их командой map-role.")
        return 0
    for row in rows:
        print(f"{row.role_code:10} ← {row.external_group}")
    return 0


async def _main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="app.cli")
    subparsers = parser.add_subparsers(dest="command", required=True)

    issue = subparsers.add_parser("issue-token", help="Выдать токен источнику")
    issue.add_argument("code", help="Код источника из event_sources")
    issue.add_argument(
        "--with-hmac", action="store_true", help="Дополнительно выдать HMAC-секрет"
    )

    admin = subparsers.add_parser(
        "create-admin", help="Создать локального администратора (или сменить пароль)"
    )
    admin.add_argument("username", help="Логин")
    admin.add_argument(
        "--password", default=None, help="Пароль; по умолчанию генерируется"
    )

    mapping = subparsers.add_parser("map-role", help="Группа каталога → роль платформы")
    mapping.add_argument("external_group", help="DN группы в каталоге")
    mapping.add_argument("role_code", help="Код роли: admin, engineer, viewer")

    unmapping = subparsers.add_parser("unmap-role", help="Удалить сопоставление группы")
    unmapping.add_argument("external_group", help="DN группы в каталоге")

    subparsers.add_parser("list-role-mappings", help="Показать сопоставления групп")

    args = parser.parse_args(argv)
    try:
        if args.command == "issue-token":
            return await _issue_token(args.code, args.with_hmac)
        if args.command == "create-admin":
            return await _create_admin(args.username, args.password)
        if args.command == "map-role":
            return await _map_role(args.external_group, args.role_code)
        if args.command == "unmap-role":
            return await _unmap_role(args.external_group)
        if args.command == "list-role-mappings":
            return await _list_role_mappings()
        return 1
    finally:
        await get_engine().dispose()


def main() -> None:
    raise SystemExit(asyncio.run(_main(sys.argv[1:])))


if __name__ == "__main__":
    main()
