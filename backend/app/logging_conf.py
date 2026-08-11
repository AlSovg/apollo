"""Структурное логирование в JSON.

Тела запросов и ответов не логируются нигде — требование по ПДн
(раздел 5 AGENTS.md): в логи не должны попадать payload событий,
адреса доставки и тела уведомлений.
"""

import json
import logging
from datetime import UTC, datetime
from typing import Any

# Штатные атрибуты LogRecord: всё остальное пришло из extra и попадёт в лог
_STANDARD_ATTRS = frozenset(
    logging.LogRecord("", 0, "", 0, "", None, None).__dict__
) | {"message", "asctime", "taskName"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _STANDARD_ATTRS and value is not None:
                payload[key] = value
        if record.exc_info and record.exc_info[0] is not None:
            payload["exc_type"] = record.exc_info[0].__name__
        return json.dumps(payload, ensure_ascii=False, default=str)


# Библиотеки, чьи INFO-логи содержат адреса. httpx печатает полный URL запроса,
# а в нём — идентификатор получателя TrueConf; ldap3 выводит DN пользователей.
# Требование ПДн: адреса доставки в логи не попадают ни от нашего кода,
# ни от зависимостей.
_ADDRESS_LEAKING_LOGGERS = ("httpx", "httpcore", "ldap3")


def setup_logging(level: str) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())
    # Стандартный access-лог uvicorn дублирует наш middleware — глушим
    logging.getLogger("uvicorn.access").disabled = True
    for name in _ADDRESS_LEAKING_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
