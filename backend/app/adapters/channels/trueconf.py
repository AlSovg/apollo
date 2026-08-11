"""Канал TrueConf — обязателен по условиям кейса.

Контракт снят с боевого TrueConf Server 5.5 (Chatbot Connector), а не с
документации: REST-отправки сообщений у сервера нет, сообщения в чат ходят
только через WebSocket. Схема:

1. POST /bridge/api/client/v1/oauth/token — JWT учётки бота (grant password;
   бот в TrueConf — это обычный пользователь сервера).
2. WebSocket /websocket/chat_bot/: auth → createP2PChat(userId) → sendMessage.

Запрос — {"type": 1, "id": N, "method": ..., "payload": ...}, ответ приходит
с тем же id и type 2; ошибки сервер кладёт в payload.errorCode (снято с
живого сервера: 201 — авторизация, 304 — нет доступа к чату). Несуществующего
получателя сервер НЕ отвергает — молча создаёт чат, поэтому опечатка в адресе
не даст ошибки доставки; адрес берётся из каталога, это принятый риск.

Соединение живёт в пределах одного send: доставка идёт последовательно с
ретраями на уровне очереди, постоянная сессия усложнила бы код ради экономии
пары рукопожатий в минуту.
"""

import asyncio
import json
import logging
import ssl
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import websockets

from app.adapters.channels.base import (
    NotificationMessage,
    PermanentChannelError,
    TransientChannelError,
)

logger = logging.getLogger(__name__)

TOKEN_PATH = "/bridge/api/client/v1/oauth/token"
WEBSOCKET_PATH = "/websocket/chat_bot/"

# Идентификатор клиентского приложения фиксирован протоколом Chatbot Connector
CLIENT_ID = "chat_bot"

_RPC_TIMEOUT_S = 15.0


@dataclass
class TrueConfSettings:
    base_url: str
    username: str
    password: str
    timeout_s: float = 10.0
    verify_tls: bool = True


@dataclass
class _BotSession:
    token: str
    expires_at: datetime
    # createP2PChat идемпотентен, но кеш экономит вызов на каждое сообщение
    chat_ids: dict[str, str] = field(default_factory=dict)


class TrueConfChannel:
    name = "trueconf"

    def __init__(self, settings: TrueConfSettings) -> None:
        self._settings = settings
        self._session: _BotSession | None = None
        self._lock = asyncio.Lock()

    # --- токен ---------------------------------------------------------------

    async def _bot_session(self) -> _BotSession:
        async with self._lock:
            if self._session and datetime.now(tz=UTC) < self._session.expires_at:
                return self._session

            try:
                async with httpx.AsyncClient(
                    base_url=self._settings.base_url,
                    timeout=self._settings.timeout_s,
                    verify=self._settings.verify_tls,
                ) as client:
                    response = await client.post(
                        TOKEN_PATH,
                        json={
                            "client_id": CLIENT_ID,
                            "grant_type": "password",
                            "username": self._settings.username,
                            "password": self._settings.password,
                        },
                    )
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                raise TransientChannelError(
                    f"TrueConf недоступен: {type(exc).__name__}"
                ) from exc

            if response.status_code >= 500:
                raise TransientChannelError(
                    f"TrueConf: ошибка сервера {response.status_code}"
                )
            # Боевой сервер отвечает 201, мок оставлен на нём же
            if response.status_code not in (200, 201):
                raise PermanentChannelError(
                    f"TrueConf: авторизация бота отклонена ({response.status_code})"
                )

            payload = response.json()
            expires_at = datetime.fromtimestamp(
                int(payload.get("expires_at", 0)), tz=UTC
            ) - timedelta(minutes=5)
            if expires_at <= datetime.now(tz=UTC):
                # Сервер не сообщил срок — обновляемся раз в сутки
                expires_at = datetime.now(tz=UTC) + timedelta(hours=24)

            self._session = _BotSession(
                token=str(payload["access_token"]), expires_at=expires_at
            )
            return self._session

    # --- WebSocket -----------------------------------------------------------

    def _websocket_url(self) -> str:
        base = self._settings.base_url.rstrip("/")
        scheme = "wss" if base.startswith("https") else "ws"
        return scheme + ":" + base.split(":", 1)[1] + WEBSOCKET_PATH

    def _ssl_context(self) -> ssl.SSLContext | None:
        if not self._settings.base_url.startswith("https"):
            return None
        context = ssl.create_default_context()
        if not self._settings.verify_tls:
            # Стенд заказчика живёт на самоподписанном сертификате
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        return context

    async def _rpc(
        self,
        connection: websockets.ClientConnection,
        request_id: int,
        method: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Один вызов протокола: сервер шлёт и события — их пропускаем."""
        await connection.send(
            json.dumps(
                {"type": 1, "id": request_id, "method": method, "payload": payload}
            )
        )
        while True:
            raw = await asyncio.wait_for(connection.recv(), timeout=_RPC_TIMEOUT_S)
            message = json.loads(raw)
            if message.get("id") == request_id and message.get("type") == 2:
                answer: dict[str, Any] = message.get("payload") or {}
                if "errorCode" in answer:
                    raise PermanentChannelError(
                        f"TrueConf: {method} отклонён (errorCode "
                        f"{answer['errorCode']})"
                    )
                return answer

    # --- отправка ------------------------------------------------------------

    async def send(self, message: NotificationMessage, address: str | None) -> None:
        if not address:
            # Идентификатор TrueConf берётся из каталога; его отсутствие —
            # проблема данных, а не связи, повторять бессмысленно
            raise PermanentChannelError("не удалось определить идентификатор получателя")

        session = await self._bot_session()

        transport_errors = (
            OSError,
            websockets.WebSocketException,
            asyncio.TimeoutError,
        )
        try:
            async with websockets.connect(
                self._websocket_url(),
                ssl=self._ssl_context(),
                open_timeout=self._settings.timeout_s,
            ) as connection:
                try:
                    await self._rpc(
                        connection,
                        1,
                        "auth",
                        {
                            "token": session.token,
                            "tokenType": "JWT",
                            "receiveUnread": False,
                        },
                    )
                except PermanentChannelError:
                    # Токен мог протухнуть раньше заявленного срока —
                    # следующий send получит новый, а этот уйдёт в ретрай
                    self._session = None
                    raise TransientChannelError(
                        "TrueConf: сессия бота не авторизована, токен сброшен"
                    ) from None

                chat_id = session.chat_ids.get(address)
                if chat_id is None:
                    answer = await self._rpc(
                        connection, 2, "createP2PChat", {"userId": address}
                    )
                    chat_id = str(answer["chatId"])
                    session.chat_ids[address] = chat_id

                await self._rpc(
                    connection,
                    3,
                    "sendMessage",
                    {
                        "chatId": chat_id,
                        "content": {"text": message.body(), "parseMode": "text"},
                    },
                )
        except transport_errors as exc:
            raise TransientChannelError(
                f"TrueConf недоступен: {type(exc).__name__}"
            ) from exc

        logger.info(
            "trueconf: уведомление доставлено",
            extra={"notification_id": message.notification_id},
        )
