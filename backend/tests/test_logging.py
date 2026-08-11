"""Логи не должны содержать адресов доставки и секретов."""

import json
import logging

from app.logging_conf import JsonFormatter, setup_logging


def test_extra_fields_are_serialised() -> None:
    formatter = JsonFormatter()
    record = logging.LogRecord(
        "app.test", logging.INFO, __file__, 1, "notification processed", None, None
    )
    record.notification_id = 42
    record.channel = "trueconf"

    payload = json.loads(formatter.format(record))

    assert payload["message"] == "notification processed"
    assert payload["notification_id"] == 42
    assert payload["channel"] == "trueconf"
    assert payload["level"] == "INFO"


def test_address_leaking_libraries_are_silenced() -> None:
    """httpx печатает полный URL запроса, а в нём идентификатор получателя."""
    setup_logging("INFO")

    for name in ("httpx", "httpcore", "ldap3"):
        assert logging.getLogger(name).level == logging.WARNING

    # Собственные логгеры приложения при этом не заглушены
    assert logging.getLogger("app.services.delivery").getEffectiveLevel() == logging.INFO
