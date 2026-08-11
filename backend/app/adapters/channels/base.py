"""Контракт канала доставки.

Новый канал (SMS, push, ServiceDesk) — это новый класс и запись в реестре;
маршрутизатор при этом не меняется.

Адрес получателя приходит параметром и нигде не сохраняется: он резолвится
в момент отправки и живёт только в памяти обработчика (FR-DLV-4).
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

SEVERITY_LABELS = {
    0: "Не классифицировано",
    1: "Информация",
    2: "Предупреждение",
    3: "Средняя",
    4: "Высокая",
    5: "Авария",
}


class ChannelError(Exception):
    """Базовая ошибка доставки."""


class TransientChannelError(ChannelError):
    """Временная ошибка: имеет смысл повторить (недоступность, 5xx, таймаут)."""


class PermanentChannelError(ChannelError):
    """Повторять бесполезно: неверный адрес, отвергнутое сообщение."""


@dataclass
class NotificationMessage:
    """Уведомление в виде, пригодном для отправки.

    Собирается из инцидента: в самой таблице notifications текста нет —
    она хранит факт доставки, а не её содержимое.
    """

    notification_id: int
    incident_id: int
    severity: int
    rule_name: str
    object_name: str | None
    opened_at: datetime
    message: str | None = None
    summary: str | None = None
    url: str | None = None

    @property
    def subject(self) -> str:
        severity = SEVERITY_LABELS.get(self.severity, str(self.severity))
        where = f" — {self.object_name}" if self.object_name else ""
        return f"[{severity}] {self.rule_name}{where}"

    def body(self) -> str:
        lines = [
            self.subject,
            f"Объект: {self.object_name or 'не определён'}",
            f"Начало: {self.opened_at:%Y-%m-%d %H:%M:%S %Z}".strip(),
        ]
        if self.summary:
            lines += ["", self.summary]
        elif self.message:
            lines += ["", self.message]
        if self.url:
            lines += ["", f"Подробнее: {self.url}"]
        return "\n".join(lines)


class NotificationChannel(Protocol):
    name: str

    async def send(self, message: NotificationMessage, address: str | None) -> None: ...
