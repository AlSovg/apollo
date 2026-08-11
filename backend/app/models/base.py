"""Базовый класс декларативных моделей.

Схема-источник истины — `db/schema.sql`. Модели повторяют её один в один,
включая имена ограничений: по ним разбираются ошибки БД в проде.
"""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass
