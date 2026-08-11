"""Канал TrueConf — обязателен по условиям кейса.

Реализация полная, а не заглушка: сообщение уходит в серверный API TrueConf
Server, авторизация — OAuth-токеном приложения, адрес сервера и реквизиты
берутся из окружения. Доступа к боевому серверу нет, поэтому проверяется
против мок-сервера, повторяющего контракт (пути, коды ответов, формат ошибок,
задержки и отказы). Переключение на бой — смена TRUECONF_BASE_URL и токена.

ВНИМАНИЕ (открытый вопрос 8 в docs/tz.md): точные пути серверного API сверены
не с боевым сервером, а с документацией протокола. Если у заказчика версия
API отличается, правится только этот файл — ядро и маршрутизатор не меняются.
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx

from app.adapters.channels.base import (
    NotificationMessage,
    PermanentChannelError,
    TransientChannelError,
)

logger = logging.getLogger(__name__)

TOKEN_PATH = "/oauth/v2/token"
MESSAGE_PATH = "/api/v3.2/chats/{user_id}/messages"


@dataclass
class TrueConfSettings:
    base_url: str
    client_id: str
    client_secret: str
    timeout_s: float = 10.0
    verify_tls: bool = True


class TrueConfChannel:
    name = "trueconf"

    def __init__(self, settings: TrueConfSettings) -> None:
        self._settings = settings
        self._token: str | None = None
        self._token_expires_at: datetime = datetime.now(tz=UTC)
        self._lock = asyncio.Lock()

    async def _access_token(self, client: httpx.AsyncClient) -> str:
        """Токен приложения кешируется до истечения срока."""
        async with self._lock:
            if self._token and datetime.now(tz=UTC) < self._token_expires_at:
                return self._token

            response = await client.post(
                TOKEN_PATH,
                data={
                    "grant_type": "client_credentials",
                    "client_id": self._settings.client_id,
                    "client_secret": self._settings.client_secret,
                },
            )
            if response.status_code >= 500:
                raise TransientChannelError(f"TrueConf: ошибка сервера {response.status_code}")
            if response.status_code != 200:
                raise PermanentChannelError(
                    f"TrueConf: авторизация отклонена ({response.status_code})"
                )

            payload = response.json()
            self._token = str(payload["access_token"])
            self._token_expires_at = datetime.now(tz=UTC) + timedelta(
                seconds=int(payload.get("expires_in", 3600)) - 60
            )
            return self._token

    async def send(self, message: NotificationMessage, address: str | None) -> None:
        if not address:
            # Идентификатор TrueConf берётся из каталога; его отсутствие —
            # проблема данных, а не связи, повторять бессмысленно
            raise PermanentChannelError("не удалось определить идентификатор получателя")

        transport_errors = (httpx.TransportError, httpx.TimeoutException)
        try:
            async with httpx.AsyncClient(
                base_url=self._settings.base_url,
                timeout=self._settings.timeout_s,
                verify=self._settings.verify_tls,
            ) as client:
                token = await self._access_token(client)
                response = await client.post(
                    MESSAGE_PATH.format(user_id=address),
                    headers={"Authorization": f"Bearer {token}"},
                    json={"type": "text", "content": {"text": message.body()}},
                )
        except transport_errors as exc:
            raise TransientChannelError(f"TrueConf недоступен: {type(exc).__name__}") from exc

        if response.status_code in (200, 201, 202):
            # В лог — только факт: ни адреса, ни текста уведомления
            logger.info("trueconf message sent", extra={"notification_id": message.notification_id})
            return

        if response.status_code == 401:
            # Токен мог протухнуть раньше срока — сбрасываем и пробуем в ретрае
            self._token = None
            raise TransientChannelError("TrueConf: токен отклонён")
        if response.status_code >= 500:
            raise TransientChannelError(f"TrueConf: ошибка сервера {response.status_code}")
        raise PermanentChannelError(f"TrueConf отклонил сообщение ({response.status_code})")
