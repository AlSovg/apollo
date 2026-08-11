"""Служебные команды бэкенда.

    python -m app.cli issue-token zabbix [--with-hmac]

Токен и HMAC-секрет печатаются один раз и в БД хранятся только в виде хеша
и секрета соответственно. В репозиторий и в логи они не попадают.
Постоянное место управления токенами — админские эндпоинты (шаг 9).
"""

import argparse
import asyncio
import secrets
import sys

from sqlalchemy import update

from app.db import get_engine, get_sessionmaker
from app.models.events import EventSource
from app.services.ingest import token_hash


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


async def _main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="app.cli")
    subparsers = parser.add_subparsers(dest="command", required=True)

    issue = subparsers.add_parser("issue-token", help="Выдать токен источнику")
    issue.add_argument("code", help="Код источника из event_sources")
    issue.add_argument(
        "--with-hmac", action="store_true", help="Дополнительно выдать HMAC-секрет"
    )

    args = parser.parse_args(argv)
    try:
        if args.command == "issue-token":
            return await _issue_token(args.code, args.with_hmac)
        return 1
    finally:
        await get_engine().dispose()


def main() -> None:
    raise SystemExit(asyncio.run(_main(sys.argv[1:])))


if __name__ == "__main__":
    main()
