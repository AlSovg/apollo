"""Универсальный приём: система шлёт готовый канонический JSON.

Самый дешёвый способ подключения (FR-SRC-1): работы ровно на создание записи
в реестре и выдачу токена, ноль строк кода.

Поле source_system из тела игнорируется: источником считается тот, чьим
токеном подписан запрос. Иначе владелец одного токена мог бы заводить события
от имени чужой системы.
"""

from datetime import tzinfo
from typing import Any

from pydantic import ValidationError

from app.adapters.sources.base import (
    AdapterError,
    CanonicalEvent,
    SeverityMap,
)


class GenericAdapter:
    """Проверяет, что payload действительно канонический, и отдаёт его как есть."""

    def __init__(
        self, severity_map: SeverityMap, timezone: tzinfo, *, source: str = "generic"
    ) -> None:
        self.source = source
        self._severity_map = severity_map
        self._timezone = timezone

    def parse(self, payload: dict[str, Any]) -> CanonicalEvent:
        body = {**payload, "source_system": self.source}
        try:
            event = CanonicalEvent.model_validate(body)
        except ValidationError as exc:
            # Понятная ошибка вместо стектрейса: интегратор должен увидеть,
            # какого поля не хватает в его каноническом JSON
            problems = "; ".join(
                f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
                for error in exc.errors()[:5]
            )
            raise AdapterError(
                f"{self.source}: тело не соответствует каноническому формату ({problems})"
            ) from exc

        if event.occurred_at.tzinfo is None:
            event.occurred_at = event.occurred_at.replace(tzinfo=self._timezone)
        return event
